import html
import json
import os
import tempfile
import requests
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv, set_key, unset_key

from collection_status import load_collection_status
from customer_pdf_report import generate_customer_pdf
from refresh_manager import (
    get_settings, save_settings, get_state, live_enabled,
    next_due, is_due, run_refresh, lock_present, overdue_notice,
)
from storage_manager import DOMAINS_FILE, SEATS_FILE, APPLICATIONS_FILE
from local_auth_audit import verify_windows_credentials, record_activity, read_activity
import streamlit.components.v1 as components
import local_idle_session as idle_lease

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
load_dotenv(dotenv_path=ENV_FILE)
STATUS_FILE = ROOT / "data" / "collection_status.json"
BRAND_FILE = ROOT / "branding.json"
HISTORY_FILE = ROOT / "data" / "history.jsonl"
RESOLUTIONS_FILE = ROOT / "data" / "resolved_backup_alerts.json"

st.set_page_config(page_title="SaaS Protection Intelligence", page_icon="🛡️", layout="wide", initial_sidebar_state="collapsed")
st.set_option("client.toolbarMode", "minimal")

DEFAULT_BRAND = {"company_name": "SaaS Protection Intelligence", "subtitle": "Customer protection, backup visibility and portfolio insights.", "logo": ""}

def read_json(path, default):
    """Read local on-disk JSON; no cloud database dependency."""
    try:
        with Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default

def date_label(value):
    if not value:
        return "Not available"
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo("Australia/Sydney")).strftime("%d %b %Y · %I:%M %p")
    except (TypeError, ValueError, OverflowError):
        return "Unknown"


def data_age(value):
    if not value:
        return "Unknown"
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        hours = max(0, (datetime.now(timezone.utc) - d.astimezone(timezone.utc)).total_seconds() / 3600)
        return f"{hours:.1f} hours"
    except (ValueError, TypeError, OverflowError):
        return "Unknown"

def coverage(active, recent):
    if (isinstance(active, (int, float)) and not isinstance(active, bool)
            and isinstance(recent, (int, float)) and not isinstance(recent, bool)
            and active > 0 and 0 <= recent <= active):
        return 100 * recent / active
    return None

def health(value):
    if value is None or pd.isna(value):
        return "⚪ Unavailable"
    if value >= 100:
        return "🟢 Healthy"
    if value >= 90:
        return "🟠 Attention"
    return "🔴 At Risk"

def csv_bytes(frame):
    return frame.to_csv(index=False).encode("utf-8-sig")

def load_store(path):
    result = read_json(path, {})
    return result if isinstance(result, dict) else {}

def customer_entry(store, customer_id):
    customers = store.get("customers", {})
    return customers.get(str(customer_id)) if isinstance(customers, dict) else None

def get_seats(store, customer_id):
    entry = customer_entry(store, customer_id)
    if not isinstance(entry, dict):
        return None, None
    seats = entry.get("seats")
    return (seats if isinstance(seats, list) else None), entry.get("last_updated")

def get_apps(store, customer_id):
    entry = customer_entry(store, customer_id)
    if not isinstance(entry, dict):
        return None, None
    pages = entry.get("applications")
    return (pages if isinstance(pages, list) else None), entry.get("last_updated")

def application_rows(pages):
    rows = []
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        for item in page.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            for suite in item.get("suites", []) or []:
                if not isinstance(suite, dict):
                    continue
                for app in suite.get("appTypes", []) or []:
                    if not isinstance(app, dict):
                        continue
                    for h in app.get("backupHistory", []) or []:
                        if not isinstance(h, dict):
                            continue
                        active = h.get("activeServiceCount")
                        recent = h.get("activeServiceWithBackupCount")
                        rows.append({
                            "Suite": suite.get("suiteType", "Unknown"),
                            "Application": app.get("appType", "Unknown"),
                            "Window": h.get("timeWindow"),
                            "Status": h.get("status"),
                            "Active Services": active,
                            "Recently Backed Up": recent,
                            "Perfect Backups": h.get("activeServiceWithPerfectBackupCount"),
                            "Coverage": coverage(active, recent)
                        })
    return rows

def daily_application_history(pages, days=90):
    """Map actual API windows to local calendar days; never infer missing statuses."""
    today = datetime.now().astimezone().date()
    earliest = today - timedelta(days=days - 1)
    result = {}
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        for item in page.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            for suite in item.get("suites", []) or []:
                if not isinstance(suite, dict):
                    continue
                for app in suite.get("appTypes", []) or []:
                    if not isinstance(app, dict):
                        continue
                    suite_name = str(suite.get("suiteType") or "Unknown")
                    app_name = str(app.get("appType") or "Unknown")
                    key = (suite_name, app_name)
                    result.setdefault(key, {})
                    for window in app.get("backupHistory", []) or []:
                        if not isinstance(window, dict):
                            continue
                        try:
                            # Datto reports timestamps in milliseconds. Use later endpoint
                            # to anchor the rolling 24-hour window to a calendar day.
                            timestamps = [float(window[k]) for k in ("startTime", "endTime")]
                            later = datetime.fromtimestamp(max(timestamps) / 1000, tz=timezone.utc)
                            day = later.astimezone().date()
                        except (KeyError, TypeError, ValueError, OverflowError, OSError):
                            continue
                        if not earliest <= day <= today:
                            continue
                        raw = str(window.get("status") or "").strip()
                        active = window.get("activeServiceCount")
                        backed = window.get("activeServiceWithBackupCount")
                        if not raw:
                            status = "No data"
                        elif raw.casefold() == "perfect":
                            status = "Perfect"
                            if (isinstance(active, (int, float)) and active > 0
                                    and isinstance(backed, (int, float)) and backed < active):
                                status = "Imperfect"

                        elif raw.casefold() in (
                                "imperfect",
                                "insufficient history",
                                "backup with issues",
                                "backup with lots of issues",
                        ):
                            status = "Imperfect"

                        elif raw.casefold() in (
                                "failed",
                                "failure",
                                "error",
                                "backup failed",
                                "backup failure",
                        ):
                            status = "Incomplete"

                        else:
                            status = "Incomplete"
                        record = {"date": day.isoformat(), "suite": suite_name,
                                  "application": app_name, "status": status,
                                  "raw_status": raw, "active": active, "backed_up": backed,
                                  "timestamp": later.timestamp(), "source_window": window}
                        previous = result[key].get(day)
                        if previous is None or record["timestamp"] > previous["timestamp"]:
                            result[key][day] = record
    return result


def render_application_timeline(pages, days=90, show_office365_placeholders=False):
    """Show a fixed 90-day grid, with grey cells for unobserved dates.

    Application placeholders are labels only; they never imply backed-up data.
    """
    history = daily_application_history(pages, days)
    dates = [datetime.now().astimezone().date() - timedelta(days=i)
             for i in range(days - 1, -1, -1)]
    names = sorted(history)
    if not names and show_office365_placeholders:
        names = [("Office365", app) for app in (
            "Office365Exchange", "Office365OneDrive",
            "Office365SharePoint", "Office365Teams")]
    if not names:
        st.info("No application inventory is available for this customer. The 90-day grid will appear when application data is collected.")
        return []

    status_code = {"No data": 0, "Incomplete": 1, "Imperfect": 2, "Perfect": 3}
    colors = [[0, "#435268"], [0.2499, "#435268"],
              [0.25, "#EF4444"], [0.4999, "#EF4444"],
              [0.5, "#F59E0B"], [0.7499, "#F59E0B"],
              [0.75, "#10B981"], [1, "#10B981"]]
    z, labels, details, observations = [], [], [], []
    for suite, app in names:
        daily = history.get((suite, app), {})
        row, hover = [], []
        for day in dates:
            observation = daily.get(day)
            status = observation["status"] if observation else "No data"
            row.append(status_code[status])
            if observation:
                observations.append(observation)
                hover.append(
                    f"{day:%d %b %Y}<br>{app.replace('Office365', '')}: {status}"
                    f"<br>Datto status: {observation['raw_status']}"
                    f"<br>Backed up: {observation['backed_up']}/{observation['active']}")
            else:
                hover.append(f"{day:%d %b %Y}<br>{app.replace('Office365', '')}: Not recorded")
        z.append(row)
        labels.append(app.replace("Office365", ""))
        details.append(hover)

    c1, c2, c3 = st.columns(3)
    c1.metric("Applications displayed", len(names))
    c2.metric("Daily observations available", len(observations))
    c3.metric("History period", f"{days} days")
    fig = go.Figure(go.Heatmap(
        z=z, x=[d.isoformat() for d in dates], y=labels,
        text=details, hovertemplate="%{text}<extra></extra>",
        zmin=0, zmax=3, colorscale=colors, showscale=False,
        xgap=2, ygap=7,
    ))
    tick_indices = list(range(0, days, 10))
    if days - 1 not in tick_indices:
        tick_indices.append(days - 1)
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#0B1120", plot_bgcolor="#0B1120",
        height=max(285, 130 + 51 * len(names)),
        margin=dict(l=10, r=12, t=30, b=55),
        xaxis=dict(tickmode="array",
                   tickvals=[dates[i].isoformat() for i in tick_indices],
                   ticktext=[dates[i].strftime("%d %b") for i in tick_indices],
                   tickangle=-45, showgrid=False, title=None),
        yaxis=dict(autorange="reversed", automargin=True, showgrid=False),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption("🟢 Perfect   🟠 Imperfect   🔴 Incomplete   ⚪ Grey = no recorded observation (not a failure). Hover over a square for its date and status.")
    if not observations:
        st.info("90-day layout ready. No application observations are saved for this customer yet; all squares are grey. Current domain-level 100% coverage does not establish historical application status.")
    return observations


def application_drilldown(pages, customer_id):
    """Interactive application -> daily backup window drill-down; snapshot-only."""
    histories = daily_application_history(pages, days=90)
    if not histories:
        return

    st.markdown("#### Application Drill-Down")
    st.caption("Select an application, then click a backup date to inspect its actual Datto history window.")
    keys = sorted(histories, key=lambda k: k[1])
    labels = [key[1].removeprefix("Office365") for key in keys]
    app_key = f"application_choice_{customer_id}"
    focus = st.session_state.get("alert_focus")
    if isinstance(focus, dict) and str(focus.get("customer_id")) == str(customer_id):
        target = (focus.get("suite"), focus.get("application"))
        if target in keys and st.session_state.get("_last_alert_radio_target") != (
            str(customer_id), target, focus.get("date")
        ):
            st.session_state[app_key] = keys.index(target)
            st.session_state["_last_alert_radio_target"] = (
                str(customer_id), target, focus.get("date")
            )
    chosen = st.radio("Application", options=list(range(len(keys))),
                      format_func=lambda index: labels[index], horizontal=True,
                      key=app_key)
    selected_key = keys[chosen]
    daily = histories[selected_key]
    ordered = sorted(daily.values(), key=lambda entry: entry["date"], reverse=True)
    if not ordered:
        st.info("No dated backup history has been recorded for this application.")
        return

    table_rows = []
    for record in ordered:
        active = record.get("active")
        backed = record.get("backed_up")
        table_rows.append({
            "Date": record["date"],
            "Backup Status": record["raw_status"] or "Unavailable",
            "Active Services": active,
            "Services Backed Up": backed,
            "Coverage": coverage(active, backed),
        })

    newest = ordered[0]
    active, backed = newest.get("active"), newest.get("backed_up")

    # Datto-style snapshot summary using only confirmed Applications API fields.
    # These are protected service counts (users/sites/teams), not file or item counts.
    app_label = labels[chosen]
    noun = "Sites" if app_label == "SharePoint" else "Teams" if app_label == "Teams" else "Users"
    perfect = newest.get("source_window", {}).get("activeServiceWithPerfectBackupCount")
    def _count(value):
        return f"{int(value):,}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "N/A"

    st.markdown(f"#### {app_label} Backup Summary")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric(f"Active {noun}", _count(active))
    s2.metric(f"{noun} Fully Protected", (
        f"{_count(perfect)}/{_count(active)}" if perfect is not None and active is not None else "N/A"
    ))
    s3.metric("Latest Backup Status", newest.get("raw_status") or "N/A")
    s4.metric("Recorded Backup Days", f"{len(ordered)} / 30")
    st.caption(f"Latest recorded backup: {newest['date']} · Counts represent protected services, not individual items.")

    st.markdown("#### Protection Metrics")
    a, b, c, d = st.columns(4)
    a.metric("Latest Status", newest.get("raw_status") or "Unknown")
    b.metric("Active Services", str(active) if active is not None else "N/A")
    c.metric("Services Backed Up", str(backed) if backed is not None else "N/A")
    cvr = coverage(active, backed)
    d.metric("Latest Coverage", f"{cvr:.1f}%" if cvr is not None else "N/A")

    st.markdown("##### Daily Backups — Select a Row")
    frame = pd.DataFrame(table_rows)
    selection = st.dataframe(
        frame, hide_index=True, use_container_width=True,
        on_select="rerun", selection_mode="single-row",
        key=f"application_daily_records_{customer_id}_{chosen}",
        column_config={"Coverage": st.column_config.ProgressColumn(
            "Coverage", min_value=0, max_value=100, format="%.1f%%")},
    )
    indices = selection.selection.rows
    if indices:
        selected_record = ordered[indices[0]]
        st.markdown(f"##### Backup Details — {selected_record['date']}")
        d1, d2, d3 = st.columns(3)
        d1.metric("Datto Status", selected_record.get("raw_status") or "Unknown")
        d2.metric("Active Services", str(selected_record.get("active") or 0))
        d3.metric("Services Backed Up", str(selected_record.get("backed_up") or 0))
        window = selected_record.get("source_window", {})
        perfect = window.get("activeServiceWithPerfectBackupCount")
        if perfect is not None:
            st.metric("Services With Perfect Backups", perfect)
        st.caption("Counts are protected services, not individual files, messages or backup jobs. "
                   "The Applications API response does not supply per-item backup counts.")
    else:
        st.caption("Click a daily backup row above to see the detailed record.")


def make_history_pdf(customer_name, observations, days=90, seat_records=None,
                     outstanding_alerts=None):
    """Customer-specific executive PDF. Counts use the same outstanding alerts as the bell.

    Data sources are existing saved Datto snapshots; no additional requests.
    """
    from io import BytesIO
    from collections import Counter
    from math import cos, sin, pi
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                   Spacer, Table, TableStyle, KeepTogether, Flowable,
                                   PageBreak)
    from xml.sax.saxutils import escape as xml_escape

    NAVY = colors.HexColor('#102033')
    BLUE = colors.HexColor('#155C9A')
    LIGHT = colors.HexColor('#F2F6FA')
    MUTED = colors.HexColor('#526577')
    LINE = colors.HexColor('#D8E2EA')
    GREEN = colors.HexColor('#10B981')
    AMBER = colors.HexColor('#F59E0B')
    RED = colors.HexColor('#EF4444')
    GREY = colors.HexColor('#D1D5DB')

    today = datetime.now().astimezone().date()
    first = today - timedelta(days=days - 1)
    all_dates = [first + timedelta(days=i) for i in range(days)]
    unique = {}
    for row in observations or []:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(str(row['date']), '%Y-%m-%d').date()
        except (KeyError, ValueError, TypeError):
            continue
        if first <= day <= today:
            key = (str(row.get('suite') or 'Unknown'),
                   str(row.get('application') or 'Unknown'), day)
            if key not in unique or float(row.get('timestamp') or 0) > float(unique[key].get('timestamp') or 0):
                unique[key] = row

    apps = sorted({(k[0], k[1]) for k in unique}, key=lambda a: a[1])
    status_counts = Counter(str(row.get('status') or 'No data') for row in unique.values())
    outstanding_alerts = outstanding_alerts if outstanding_alerts is not None else []
    outstanding = len(outstanding_alerts)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='SaaSExecTitle', parent=styles['Title'], fontName='Helvetica-Bold',
                              fontSize=19, leading=23, textColor=NAVY, alignment=TA_LEFT, spaceAfter=7))
    styles.add(ParagraphStyle(name='SaaSExecSection', parent=styles['Heading2'],
                              fontName='Helvetica-Bold', fontSize=12, leading=16,
                              textColor=BLUE, spaceBefore=12, spaceAfter=7, keepWithNext=1))
    styles.add(ParagraphStyle(name='SaaSExecBody', parent=styles['BodyText'], fontSize=8.5,
                              leading=12, textColor=NAVY, spaceAfter=6))
    styles.add(ParagraphStyle(name='SaaSExecSmall', parent=styles['BodyText'], fontSize=7.5,
                              leading=10, textColor=MUTED, spaceAfter=5))
    styles.add(ParagraphStyle(name='SaaSExecCell', parent=styles['BodyText'], fontSize=7.6,
                              leading=10.5, textColor=NAVY))
    styles.add(ParagraphStyle(name='SaaSExecHeader', parent=styles['BodyText'],
                              fontName='Helvetica-Bold', fontSize=7.5, leading=10,
                              textColor=colors.white))
    styles.add(ParagraphStyle(name='SaaSChartTitle', parent=styles['Heading3'],
                              fontName='Helvetica-Bold', fontSize=11.5, leading=15,
                              textColor=NAVY, spaceBefore=10, spaceAfter=5))

    def para(value, style='SaaSExecCell'):
        return Paragraph(xml_escape(str(value if value is not None else 'N/A')),
                         styles[style])

    buffer = BytesIO()
    page_width, page_height = landscape(A4)
    margin = 36
    usable = page_width - 2 * margin
    doc = BaseDocTemplate(buffer, pagesize=landscape(A4), leftMargin=margin,
                          rightMargin=margin, topMargin=50, bottomMargin=46,
                          title=f'{customer_name} - Application Backup History',
                          author='SaaS Protection Intelligence')
    frame = Frame(margin, 46, usable, page_height - 96,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(margin, page_height - 32, page_width - margin, page_height - 32)
        canvas.line(margin, 35, page_width - margin, 35)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(margin, page_height - 25, 'SaaS Protection Intelligence')
        canvas.drawString(margin, 23, 'Customer-specific report | Saved Datto API observations')
        canvas.drawRightString(page_width - margin, 23, f'Page {document.page}')
        canvas.restoreState()

    doc.addPageTemplates(PageTemplate(id='Executive', frames=[frame], onPage=decorate))
    story = [para('Application Backup History Report', 'SaaSExecTitle'),
             para(f'{customer_name}  |  Executive backup intelligence', 'SaaSExecBody'),
             para(f'Report period: {first:%d/%m/%Y} – {today:%d/%m/%Y}  |  '
                  f'Generated: {datetime.now().astimezone():%d/%m/%Y %I:%M %p}', 'SaaSExecSmall')]

    def section(title):
        story.append(para(title, 'SaaSExecSection'))

    def styled_table(headers, rows, widths):
        if not rows:
            story.append(para('No recorded data available.', 'SaaSExecSmall'))
            return
        table = Table([[para(v, 'SaaSExecHeader') for v in headers]] +
                      [[para(v) for v in row] for row in rows],
                      colWidths=widths, repeatRows=1, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), NAVY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT]),
            ('GRID', (0, 0), (-1, -1), .35, LINE),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 7),
            ('RIGHTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(table)

    class Donut(Flowable):
        """Native ReportLab vector donut; no external images or dependencies."""
        def __init__(self, data, labels, palette, diameter=142):
            super().__init__()
            self.data, self.labels, self.palette = data, labels, palette
            self.diameter = diameter
            self.width, self.height = usable, diameter + 18

        def draw(self):
            canvas = self.canv
            r = self.diameter / 2
            x, y = 110, r + 8
            total = sum(self.data)
            start = 90.0
            for amount, color in zip(self.data, self.palette):
                if total <= 0 or amount <= 0:
                    continue
                angle = 360.0 * amount / total
                canvas.setFillColor(color)
                canvas.wedge(x-r, y-r, x+r, y+r, start, -angle, stroke=0, fill=1)
                start -= angle
            canvas.setFillColor(colors.white)
            canvas.circle(x, y, r * 0.54, stroke=0, fill=1)
            canvas.setFillColor(NAVY)
            canvas.setFont('Helvetica-Bold', 13)
            center = str(total) if total else 'N/A'
            canvas.drawCentredString(x, y+1, center)
            canvas.setFont('Helvetica', 7)
            canvas.drawCentredString(x, y-12, 'services / seats')
            legend_x = 237
            for i, (label, amount, color) in enumerate(zip(self.labels, self.data, self.palette)):
                ly = self.height - 30 - i * 26
                canvas.setFillColor(color)
                canvas.roundRect(legend_x, ly-2, 12, 12, 2, stroke=0, fill=1)
                canvas.setFillColor(NAVY)
                canvas.setFont('Helvetica', 9)
                pct = f'{100 * amount / total:.1f}%' if total else 'N/A'
                canvas.drawString(legend_x + 21, ly, f'{label}: {amount} ({pct})')

    def safe_count(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return int(value)
        return None

    def app_label(name):
        return name.removeprefix('Office365')

    # 1. Executive summary: outstanding count must match the same bell list.
    section('1. Executive Summary')
    summary_rows = [
        ('Client Name', customer_name),
        ('Applications Monitored', len(apps)),
        ('Reporting Period', f'{first:%d/%m/%Y} – {today:%d/%m/%Y} ({days} calendar days)'),
        ('Recorded Application-Day Observations', len(unique)),
        ('Perfect / Imperfect / Incomplete Observations',
         f"{status_counts['Perfect']} / {status_counts['Imperfect']} / {status_counts['Incomplete']}"),
        ('Outstanding Backup Alerts (last 30 days)', outstanding),
    ]
    styled_table(['Measure', 'Value'], summary_rows, [280, usable - 280])
    story.append(Spacer(1, 6))
    story.append(para('Outstanding alerts exclude operator-resolved observations and use the same '
                      '30-day criteria as the notification bell. This report is customer-specific; '
                      'the dashboard bell may include other customers. Historical imperfect '
                      'observations remain in the timeline.', 'SaaSExecSmall'))

    # Seat Intelligence snapshot mirrors existing customer Seat Intelligence UI.
    section('Seat Intelligence')
    if seat_records is None:
        story.append(para('Seat inventory was not available in the saved API snapshot.', 'SaaSExecSmall'))
    else:
        seats = [x for x in seat_records if isinstance(x, dict)]
        seat_types = Counter(str(x.get('seatType') or 'Unknown') for x in seats)
        ordered_types = sorted(seat_types.items(), key=lambda kv: (-kv[1], kv[0]))
        def billing_class(value):
            if value is None or str(value).strip() == '':
                return 'Unknown'
            val = str(value).strip().lower()
            return 'Billable' if val in ('1', 'true', 'yes') else 'Non-Billable' if val in ('0', 'false', 'no') else 'Unknown'
        billing = Counter(billing_class(s.get('billable')) for s in seats)
        story.append(para(f'Seats retrieved: {len(seats)}  |  Billable: {billing["Billable"]}  |  '
                          f'Non-Billable: {billing["Non-Billable"]}' +
                          (f'  |  Billing unknown: {billing["Unknown"]}' if billing['Unknown'] else ''),
                          'SaaSExecBody'))
        if ordered_types:
            palette = [colors.HexColor(c) for c in ('#6366F1','#F0523A','#10B981','#A855F7','#FB923C','#60A5FA','#A3A3A3')]
            story.append(Donut([n for _, n in ordered_types],
                               [label for label, _ in ordered_types],
                               [palette[i % len(palette)] for i in range(len(ordered_types))]))

    # Per-application visual summaries are placed before the protection table.
    order = {'Exchange': 0, 'OneDrive': 1, 'SharePoint': 2, 'Teams': 3}
    apps = sorted(apps, key=lambda pair: (order.get(app_label(pair[1]), 99), pair[1]))
    for suite, app in apps:
        records = sorted((r for (s, a, d), r in unique.items() if (s, a) == (suite, app)),
                         key=lambda r: r['date'], reverse=True)
        latest = records[0]
        name = app_label(app)
        active = safe_count(latest.get('active'))
        backed = safe_count(latest.get('backed_up'))
        perfect = safe_count((latest.get('source_window') or {}).get('activeServiceWithPerfectBackupCount'))
        valid = active is not None and backed is not None and active > 0 and 0 <= backed <= active
        noun = 'Sites' if name == 'SharePoint' else 'Teams' if name == 'Teams' else 'Users'
        app_block = [para(f'{name} Backup Summary', 'SaaSChartTitle')]
        metrics = (f'Active {noun}: {active if active is not None else "N/A"}   |   '
                   f'{noun} Fully Protected: '
                   f'{str(perfect) + "/" + str(active) if perfect is not None and active is not None else "N/A"}'
                   f'   |   Latest Datto Status: {latest.get("raw_status") or "N/A"}')
        app_block.append(para(metrics, 'SaaSExecBody'))
        if valid:
            app_block.append(Donut([backed, active - backed],
                               ['Recent backup', 'Without recent backup'],
                               [GREEN, AMBER]))
        else:
            app_block.append(para('Recent-backup coverage is unavailable for this application.', 'SaaSExecSmall'))
        app_block.append(para(f'Latest observed: {latest["date"]}  |  Observed days: {len(records)}. '
                          'Coverage is per protected service, not individual backup items. '
                          'A Datto issue can exist even at 100% recent-backup coverage.', 'SaaSExecSmall'))
        story.append(KeepTogether(app_block))

    section('2. Application Protection Summary')
    app_summaries = []
    for suite, app in apps:
        records = sorted((r for (s, a, d), r in unique.items() if (s, a) == (suite, app)),
                         key=lambda r: r['date'], reverse=True)
        latest = records[0]
        active, backed = latest.get('active'), latest.get('backed_up')
        fraction = f'{backed}/{active}' if active is not None and backed is not None else 'N/A'
        app_unresolved = sum(str(a.get('application')) == app and
                             str(a.get('suite')) == suite for a in outstanding_alerts)
        app_summaries.append((app_label(app), latest['date'], latest.get('raw_status') or 'Unknown',
                              fraction, len(records), app_unresolved))
    styled_table(['Application', 'Latest observed', 'Datto status', 'Backed up / active',
                  'Observed days', 'Open alerts'],
                 app_summaries, [130, 110, 165, 134, 122, usable - 661])
    story.append(para('Open alerts use the same unresolved 30-day alert records as the notification bell; '
                      'these are not the number of historically imperfect days.', 'SaaSExecSmall'))

    section('3. Application Backup Timeline — 90 Days')
    story.append(para('Green: Perfect  |  Amber: Imperfect  |  Red: Incomplete  |  '
                      'Grey: No saved observation (not a failure).', 'SaaSExecSmall'))
    color_by_status = {'Perfect': GREEN, 'Imperfect': AMBER, 'Incomplete': RED, 'No data': GREY}
    for offset in range(0, days, 15):
        segment = all_dates[offset:offset+15]
        month_header = ['Application'] + [day.strftime('%d') for day in segment]
        matrix = [month_header] + [[app_label(app)] + ['']*len(segment) for _, app in apps]
        if not apps:
            continue
        col_width = (usable - 138) / len(segment)
        timeline = Table(matrix, colWidths=[138] + [col_width]*len(segment),
                         rowHeights=[25] + [22]*len(apps))
        rules = [('BACKGROUND', (0, 0), (-1, 0), NAVY),
                 ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                 ('FONTSIZE', (0, 0), (-1, -1), 7.5),
                 ('GRID', (0, 0), (-1, -1), .35, LINE),
                 ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                 ('LEFTPADDING', (0, 0), (-1, -1), 5)]
        for ri, (suite, app) in enumerate(apps, 1):
            for ci, day in enumerate(segment, 1):
                observed = unique.get((suite, app, day))
                status = str(observed.get('status') or 'No data') if observed else 'No data'
                rules.append(('BACKGROUND', (ci, ri), (ci, ri), color_by_status.get(status, GREY)))
        timeline.setStyle(TableStyle(rules))
        story.append(KeepTogether([para(f'{segment[0]:%d/%m/%Y} – {segment[-1]:%d/%m/%Y}',
                                         'SaaSExecBody'), timeline, Spacer(1, 8)]))

    section('4. Reporting Notes')
    story.append(para('This report is generated only from saved Datto SaaS Protection snapshots. '
                      'A 90-day layout does not imply 90 days of API history. '
                      'Missing dates stay grey; no individual failed mailbox, site or item is inferred.',
                      'SaaSExecBody'))
    story.append(para('Provided as-is for informational use. Kaseya provides no support for this tool.',
                      'SaaSExecSmall'))
    doc.build(story)
    return buffer.getvalue()



def draw_coverage(frame, label_column, title):
    if frame.empty:
        st.info("No valid coverage data available.")
        return
    frame = frame.sort_values("Coverage")
    fig = go.Figure(go.Bar(
        x=frame["Coverage"], y=frame[label_column], orientation="h",
        marker_color=["#34D399" if x >= 100 else "#FBBF24" if x >= 90 else "#F87171" for x in frame["Coverage"]],
        text=frame["Coverage"].map(lambda x: f"{x:.1f}%"), textposition="outside",
        hovertemplate="%{y}<br>Recent backup coverage: %{x:.1f}%<extra></extra>"
    ))
    fig.update_layout(title=title, template="plotly_dark", paper_bgcolor="#0B1120", plot_bgcolor="#0B1120",
                      height=max(280, len(frame) * 52 + 100), margin=dict(l=15, r=65, t=50, b=30),
                      xaxis=dict(range=[0, 110], ticksuffix="%", gridcolor="#253248"),
                      yaxis=dict(automargin=True), showlegend=False)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

def export_button(title, frame, filename, key):
    if frame is not None and not frame.empty:
        st.download_button(title, csv_bytes(frame), file_name=filename, mime="text/csv", key=key)
    else:
        st.caption(f"{title}: no collected records available")

# Snapshot-only backup failure notifications. Only explicit Datto failure statuses count.
# Missing observations and imperfect/partial protection do not imply failed backups.
CONFIRMED_BACKUP_FAILURES = {
    "failed",
    "failure",
    "error",
    "backup failed",
    "backup failure",
    "backup with issues",
    "backup with lots of issues",
}


def backup_alert_key(alert):
    """Stable identity of one dated Datto application observation."""
    return json.dumps([
        str(alert["customer_id"]), str(alert["suite"]),
        str(alert["application"]), str(alert["date"]),
    ], separators=(",", ":"), ensure_ascii=False)


def load_alert_resolutions():
    """Fail visibly and retain alerts when the resolution journal is unreadable."""
    if not RESOLUTIONS_FILE.exists():
        return {"resolved": {}, "history": []}
    try:
        with RESOLUTIONS_FILE.open("r", encoding="utf-8") as source:
            result = json.load(source)
        if not isinstance(result, dict) or not isinstance(result.get("resolved"), dict) or not isinstance(result.get("history"), list):
            raise ValueError("Invalid resolution journal structure")
        return result
    except (OSError, ValueError, TypeError) as exc:
        st.error(f"Unable to read saved alert resolutions ({type(exc).__name__}). No alerts will be hidden until the journal is repaired.")
        return None


def persist_alert_resolution(alert):
    """Atomically persist one acknowledged resolution with its audit details."""
    state = load_alert_resolutions()
    if state is None:
        return False
    key = backup_alert_key(alert)
    if key in state["resolved"]:
        return True
    event = {
        "alert_key": key,
        "customer_id": str(alert["customer_id"]),
        "customer": str(alert["customer"]),
        "suite": str(alert["suite"]),
        "application": str(alert["application"]),
        "backup_date": str(alert["date"]),
        "datto_status": str(alert["status"]),
        "resolved_at_utc": datetime.now(timezone.utc).isoformat(),
        "action": "operator_marked_resolved",
        "resolved_by": st.session_state.get("authenticated_windows_user", "UNKNOWN"),
    }
    state["resolved"][key] = event
    state["history"].append(event)
    temporary = None
    try:
        RESOLUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=RESOLUTIONS_FILE.parent, prefix=".alert-resolutions-",
                                         suffix=".tmp", encoding="utf-8", delete=False) as out:
            temporary = Path(out.name)
            json.dump(state, out, indent=2, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(str(temporary), str(RESOLUTIONS_FILE))
        return True
    except OSError as exc:
        st.error(f"Resolution was NOT saved ({type(exc).__name__}). The alert remains outstanding.")
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@st.dialog("Confirm backup alert resolution", dismissible=False)
def confirm_backup_resolution(alert):
    st.markdown(
        f"**{alert['customer']} · "
        f"{alert['application'].removeprefix('Office365')}**"
    )

    st.caption(
        f"Backup observation: {alert['date']} · "
        f"Datto status: {alert['status']}"
    )

    st.warning(
        "This clears only this dated alert from the outstanding bell count. "
        "It does not change Datto backup status or verify remediation automatically."
    )

    with st.form("backup_resolution_confirmation", clear_on_submit=False):
        verified = st.checkbox(
            "I have verified that this backup issue is resolved."
        )

        left, right = st.columns(2)

        with left:
            cancelled = st.form_submit_button(
                "Cancel",
                use_container_width=True
            )

        with right:
            confirmed = st.form_submit_button(
                "Confirm Resolution",
                type="primary",
                use_container_width=True
            )

    if cancelled:
        st.session_state.pop("pending_resolution_alert", None)
        st.rerun()

    if confirmed:
        if not verified:
            st.error("Please verify the backup issue before confirming.")
            return

        if persist_alert_resolution(alert):
            try:
                record_activity(
                    st.session_state["authenticated_windows_user"],
                    "ALERT_RESOLVED",
                    target=(
                        f"{alert['customer']} · "
                        f"{alert['application']}"
                    ),
                    alert_id=backup_alert_key(alert),
                    details=(
                        f"Backup observation {alert['date']}; "
                        f"Datto status {alert['status']}"
                    ),
                )
            except OSError:
                st.error(
                    "Resolution saved, but the activity audit "
                    "could not be written. Contact the administrator."
                )
                st.stop()

            st.session_state.pop("alert_focus", None)
            st.session_state.pop("pending_resolution_alert", None)
            st.rerun()


def backup_failure_notifications(domain_snapshot, application_snapshot):
    """One alert per customer/application/calendar date, newest first (30-day window)."""
    names = {}
    for domain in domain_snapshot.get("domains", []) or []:
        if isinstance(domain, dict):
            customer_id = domain.get("saasCustomerId")
            if customer_id is not None:
                names[str(customer_id)] = str(
                    domain.get("saasCustomerName") or domain.get("organizationName")
                    or f"Customer {customer_id}"
                )
    entries = application_snapshot.get("customers", {})
    if not isinstance(entries, dict):
        return []
    unique = {}
    for customer_id, entry in entries.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("applications"), list):
            continue
        # Reuse the existing timestamp-to-local-day conversion and de-duplication.
        for (suite, app), days in daily_application_history(
            entry["applications"], days=30
        ).items():
            for day, observation in days.items():
                reported = str(observation.get("raw_status") or "").strip()
                if reported.casefold() not in CONFIRMED_BACKUP_FAILURES:
                    continue
                key = (str(customer_id), suite, app, day.isoformat())
                unique[key] = {
                    "customer_id": str(customer_id),
                    "customer": names.get(str(customer_id), f"Customer {customer_id}"),
                    "suite": suite,
                    "application": app,
                    "date": day.isoformat(),
                    "status": reported,
                    "record": observation,
                }
    return sorted(unique.values(), key=lambda a: (
        a["date"], a["customer"], a["application"]
    ), reverse=True)


def customer_backup_status_distribution(domain_frame, application_snapshot):
    """Classify each customer from saved Datto snapshots using the agreed policy.

    The *custom rule* counts active protected SERVICES missing a recent backup,
    not failed backup items or confirmed failed jobs. Datto defines the recent
    window; the scheduler's eight-hour cadence does not define that window.
    No live API calls or writes occur here. Each dashboard render evaluates the
    latest locally saved snapshots produced by the existing refresh workflow.
    """
    categories = ["Perfect", "Failed", "Imperfect", "Unavailable"]
    totals = {name: 0 for name in categories}
    details = []
    if domain_frame.empty:
        return totals, details

    unique = domain_frame[["Customer ID", "Customer"]].drop_duplicates(subset=["Customer ID"])
    failures = {"failed", "failure", "error", "backup failed", "backup failure"}
    issues = {"imperfect", "backup with issues", "backup with lots of issues", "insufficient history"}

    for _, customer in unique.iterrows():
        cid = customer["Customer ID"]
        name = str(customer["Customer"])
        domains = domain_frame[domain_frame["Customer ID"].astype(str) == str(cid)]
        domain_missing = False
        missing_recent = 0
        evaluated_services = 0
        for _, row in domains.iterrows():
            active = row.get("Active Services")
            recent = row.get("Recently Backed Up")
            if (not isinstance(active, (int, float)) or isinstance(active, bool)
                    or not isinstance(recent, (int, float)) or isinstance(recent, bool)
                    or pd.isna(active) or pd.isna(recent)
                    or active < 0 or recent < 0 or recent > active
                    or int(active) != active or int(recent) != recent):
                domain_missing = True
                continue
            evaluated_services += int(active)
            missing_recent += int(active - recent)

        pages, _ = get_apps(application_snapshot, cid)
        newest = {}
        if pages:
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for item in page.get("items", []) or []:
                    if not isinstance(item, dict):
                        continue
                    for suite in item.get("suites", []) or []:
                        if not isinstance(suite, dict):
                            continue
                        for app in suite.get("appTypes", []) or []:
                            if not isinstance(app, dict):
                                continue
                            app_key = (str(suite.get("suiteType") or ""),
                                       str(app.get("appType") or ""))
                            for history in app.get("backupHistory", []) or []:
                                if not isinstance(history, dict):
                                    continue
                                try:
                                    ended = float(history.get("endTime"))
                                except (TypeError, ValueError, OverflowError):
                                    continue
                                if ended > 0 and (app_key not in newest or ended > newest[app_key][0]):
                                    newest[app_key] = (ended, history)

        app_states = []
        for _, history in newest.values():
            raw = str(history.get("status") or "").strip().casefold()
            if raw in failures:
                app_states.append("Failed")
            elif raw in issues:
                app_states.append("Imperfect")
            elif raw == "perfect":
                app_active = history.get("activeServiceCount")
                backed = history.get("activeServiceWithBackupCount")
                if (isinstance(app_active, (int, float)) and not isinstance(app_active, bool)
                        and isinstance(backed, (int, float)) and not isinstance(backed, bool)
                        and app_active > 0 and 0 <= backed < app_active):
                    app_states.append("Imperfect")
                else:
                    app_states.append("Perfect")
            else:
                app_states.append("Unavailable")

        # Precedence: Datto explicit failure > custom threshold > incomplete
        # evidence > custom 1-9 gap > Datto issues > fully covered/Perfect.
        # Never describe a missing-recent-backup service as a Datto-confirmed
        # failed job or as a failed *item*.
        if "Failed" in app_states:
            status, reason = "Failed", "Datto-reported failure (application history)"
            source = "Datto"
        elif missing_recent > 9:
            status = "Failed"
            reason = (f"Custom Rule: {missing_recent}/{evaluated_services} active services "
                      "without a recent backup (threshold: >9)")
            source = "Custom Rule"
        elif domain_missing or evaluated_services == 0:
            status, reason = "Unavailable", "Data Quality: recent-service coverage missing or no active services established"
            source = "Data Quality"
        elif not app_states or "Unavailable" in app_states:
            status, reason = "Unavailable", "Data Quality: application history missing or inconclusive"
            source = "Data Quality"
        elif missing_recent > 0:
            status = "Imperfect"
            reason = (f"Custom Rule: {missing_recent}/{evaluated_services} active services "
                      "without a recent backup (1–9)")
            source = "Custom Rule"
        elif "Imperfect" in app_states:
            status, reason = "Imperfect", "Datto-reported backup issues (application history)"
            source = "Datto"
        else:
            status, reason = "Perfect", "Datto-reported Perfect; all active services have recent backup coverage"
            source = "Datto + Coverage"

        totals[status] += 1
        details.append({"Customer ID": str(cid), "Customer": name, "Status": status,
                        "Reason": reason, "Classification Source": source,
                        "Active Services": evaluated_services,
                        "No Recent Backup": missing_recent})
    return totals, details

def render_backup_status_donut(domain_frame, application_snapshot):
    st.subheader("Backup Status Distribution")
    totals, details = customer_backup_status_distribution(domain_frame, application_snapshot)
    total_customers = sum(totals.values())
    if not total_customers:
        st.info("No customers available to calculate backup status distribution.")
        return
    categories = ["Perfect", "Failed", "Imperfect", "Unavailable"]
    colors = ["#10B981", "#EF4444", "#F59E0B", "#B7A7CA"]
    values = [totals[c] for c in categories]
    percent = [100.0 * v / total_customers for v in values]
    fig = go.Figure(go.Pie(
        labels=categories, values=values, hole=0.60,
        marker=dict(colors=colors), sort=False, direction="clockwise",
        textinfo="percent", textposition="inside",
        hovertemplate="%{label}: %{value} customer(s) · %{percent}<extra></extra>",
        showlegend=True,
    ))
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#0B1120", plot_bgcolor="#0B1120",
        height=360, margin=dict(l=15, r=15, t=10, b=10),
        legend=dict(orientation="v", x=0.76, y=0.55),
        annotations=[dict(text=f"{total_customers}<br>customers", x=0.5, y=0.5,
                          showarrow=False, font=dict(size=17, color="#F8FAFC"))],
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption(
        "Customer-based percentage (each customer counted once), not the percentage of individual backup runs. "
        "Based on saved Datto Domains recent-backup coverage and latest application backup windows. "
        "Custom Rule: more than 9 active services without a recent backup = Failed; 1–9 = Imperfect. "
        "This is a coverage-policy classification, not a Datto-confirmed failed job or failed-item count. "
        "Missing data is Unavailable, not a failure. Collection failures do not imply backup failures. "
        "Snapshot results may be stale."
    )
    with st.expander("View status counts and exact percentages"):
        st.dataframe(pd.DataFrame({
            "Status": categories, "Customers": values,
            "Percentage": [f"{p:.6f}%" for p in percent],
        }), hide_index=True, use_container_width=True)
        st.caption(f"Total: {total_customers} customers · 100% (unrounded).")
        if details:
            st.dataframe(pd.DataFrame(details), hide_index=True, use_container_width=True)


# Phase 1B — authenticated browser-tab lease, persisted across F5 in sessionStorage.
# The server is authoritative; JavaScript events never create sessions or validate passwords.
idle_widget = components.declare_component("saas_local_idle", path=str(ROOT / "local_idle_component"))


def _audit_logout(actor, action):
    try:
        record_activity(actor, action, details=("Inactivity limit reached: 15 minutes" if action == "SESSION_TIMEOUT" else "Operator signed out"))
        return True
    except OSError:
        st.error("Could not write mandatory session audit event; application access is blocked.")
        return False


def _clear_operator():
    st.session_state.pop("authenticated_windows_user", None)
    st.session_state.pop("authenticated_lease_token", None)
    # Never restore a resolution dialog after sign-out/session expiration.
    st.session_state.pop("pending_resolution_alert", None)
    st.session_state.pop("pending_api_credentials", None)
    st.session_state.pop("api_credential_stage", None)


lease_token = st.session_state.get("authenticated_lease_token", "")
# Component appears even when unauthenticated so it can recover a lease on browser refresh.
widget_event = idle_widget(
    issue=lease_token,
    authorized=bool(lease_token),
    clear=bool(st.session_state.pop("idle_clear_token", False)),
    stay_ack=int(st.session_state.get("idle_stay_ack", 0)),
    remaining_seconds=idle_lease.remaining(lease_token) if lease_token else 0,
    key="saas_idle_session_bridge",
    default=None,
)
widget_event = widget_event if isinstance(widget_event, dict) else {}
kind = str(widget_event.get("kind", ""))
received_token = str(widget_event.get("token", ""))
nonce = str(widget_event.get("nonce", ""))
event_id = (kind, received_token, nonce)
new_event = bool(kind) and event_id != st.session_state.get("idle_last_event")
if new_event:
    st.session_state["idle_last_event"] = event_id

# Browser refresh destroys Streamlit session_state but preserves this tab's storage.
if not lease_token and new_event and kind == "BOOT" and received_token:
    actor, expired = idle_lease.check(received_token)
    if actor and not expired:
        st.session_state["authenticated_windows_user"] = actor
        st.session_state["authenticated_lease_token"] = received_token
        st.rerun()
    if expired:
        _audit_logout(actor, "SESSION_TIMEOUT")
    st.session_state["idle_clear_token"] = True
    st.rerun()

if lease_token:
    actor, expired = idle_lease.check(lease_token)
    if expired or not actor:
        if expired and actor:
            _audit_logout(actor, "SESSION_TIMEOUT")
        _clear_operator()
        st.session_state["idle_clear_token"] = True
        st.rerun()
    # Ignore events referring to another token or stale component events.
    if new_event and received_token == lease_token:
        if kind in ("ACTIVITY", "STAY"):
            checked_actor, checked_expired = idle_lease.check(lease_token, touch=True)
            if not checked_actor or checked_expired:
                if checked_expired and checked_actor:
                    _audit_logout(checked_actor, "SESSION_TIMEOUT")
                _clear_operator()
                st.session_state["idle_clear_token"] = True
                st.rerun()
            if kind == "STAY":
                st.session_state["idle_stay_ack"] = st.session_state.get("idle_stay_ack", 0) + 1
        elif kind == "TIMEOUT":
            # Client-side clock is advisory; do NOT expire a still-valid server session.
            if idle_lease.remaining(lease_token) <= 0:
                _audit_logout(actor, "SESSION_TIMEOUT")
                idle_lease.invalidate(lease_token)
                _clear_operator()
                st.session_state["idle_clear_token"] = True
                st.rerun()
    st.session_state["authenticated_windows_user"] = actor

# Windows account authentication required before reading or rendering customer data.
if not st.session_state.get("authenticated_lease_token"):
    st.title("🔐 SaaS Protection Intelligence")
    st.subheader("Windows sign-in")
    st.caption("Sign in with your Windows or domain account. Credentials are verified by Windows and are not saved.")
    with st.form("windows_signin", clear_on_submit=True):
        username = st.text_input("Windows username", placeholder=r"DOMAIN\username or local username")
        password = st.text_input("Windows password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            actor = verify_windows_credentials(username, password)
            if actor:
                record_activity(actor, "LOGIN", details="Successful interactive Windows sign-in")
                st.session_state["authenticated_windows_user"] = actor
                st.session_state["authenticated_lease_token"] = idle_lease.create(actor)
                st.session_state.pop("idle_clear_token", None)
                st.rerun()
            else:
                st.error("Windows sign-in failed. Use your qualified DOMAIN\\username and password.")
        except OSError:
            st.error("Audit storage unavailable. Login blocked to avoid unaudited activity.")
    st.stop()

# Load stored snapshots before the header; notification rendering makes no API calls.
domain_store = load_store(DOMAINS_FILE)
seats_store = load_store(SEATS_FILE)
apps_store = load_store(APPLICATIONS_FILE)
all_backup_alerts = backup_failure_notifications(domain_store, apps_store)
alert_resolution_state = load_alert_resolutions()
backup_alerts = (
    [alert for alert in all_backup_alerts
     if backup_alert_key(alert) not in alert_resolution_state["resolved"]]
    if alert_resolution_state is not None else all_backup_alerts
)

brand = {**DEFAULT_BRAND, **load_store(BRAND_FILE)}
st.markdown("""
<style>
/* Align Refresh Now with the notification bell */
div.st-key-refresh_now_header {
    margin-top: 0 !important;
}
/* All three header actions share one horizontal row and a common 46px height. */
div.st-key-local_windows_signout button {
    min-height: 46px !important;
    width: 100% !important;
    white-space: nowrap;
}
[data-testid="stPopover"] > button {
    min-height: 46px !important;
    width: 100% !important;
    white-space: nowrap;
}
/* Refresh Now - Green Button */
div.st-key-refresh_now_header button {
    background-color: #10B981 !important;
    color: #FFFFFF !important;
    border: 1px solid #10B981 !important;
    border-radius: 9px !important;
    min-height: 46px !important;
    width: 100% !important;
    font-weight: 700 !important;
    font-size: 15px !important;
    opacity: 1 !important;
}

div.st-key-refresh_now_header button:hover:not(:disabled) {
    background-color: #059669 !important;
    border-color: #059669 !important;
}

div.st-key-refresh_now_header button:disabled {
    background-color: #065F46 !important;
    border-color: #047857 !important;
    color: #D1FAE5 !important;
    opacity: 1 !important;
    cursor: not-allowed !important;
}
.stApp {background:#0B1120;color:#E8EFF9}
.block-container {max-width:1800px;padding:1.7rem 3rem 3rem}
h1,h2,h3 {color:#F1F5F9!important}
[data-testid="stMetric"] {background:#151F31;border:1px solid #28364D;border-radius:14px;padding:19px 22px;min-height:115px}
[data-testid="stMetricLabel"] {color:#A7B6CB}
[data-testid="stMetricValue"] {color:#F8FAFC;font-weight:700}
[data-testid="stDataFrame"] {border:1px solid #28364D;border-radius:12px}
.stButton button,.stDownloadButton button {border-radius:9px}
/* Bell remains prominent at zero; failures are highlighted in red. */
[data-testid="stPopover"] > button {border:1px solid #9F3141!important; color:#FDA4AF!important; font-weight:700!important;}
.stDownloadButton button[kind="primary"] {background:#2563EB}
hr {border-color:#28364D!important}
</style>
""", unsafe_allow_html=True)

logo = brand.get("logo", "")
head_a, head_b, head_c = st.columns([1, 6, 3], vertical_alignment="center")
with head_a:
    if logo:
        logo_path = ROOT / Path(logo).name
        if logo_path.is_file():
            st.image(str(logo_path), width=95)
with head_b:
    st.title(str(brand.get("company_name") or DEFAULT_BRAND["company_name"]))
    st.caption(str(brand.get("subtitle") or DEFAULT_BRAND["subtitle"]))
with head_c:
    st.caption(f"Signed in: {st.session_state['authenticated_windows_user']}")
    bell_column, refresh_column, signout_column = st.columns(
        [1.15, 2.0, 1.05], gap="small", vertical_alignment="center"
    )
    with signout_column:
        if st.button("Sign out", key="local_windows_signout", use_container_width=True):
            try:
                record_activity(st.session_state["authenticated_windows_user"], "LOGOUT")
            except OSError:
                st.error("Unable to write sign-out audit event; please contact the administrator.")
                st.stop()
            idle_lease.invalidate(st.session_state.get("authenticated_lease_token", ""))
            _clear_operator()
            st.session_state["idle_clear_token"] = True
            st.rerun()
    with bell_column:
        alert_count = len(backup_alerts)

        # Always display the alert count when backup issues exist.
        bell_label = f"🔔 🔴 {alert_count}" if alert_count > 0 else "🔔"

        with st.popover(bell_label):
            st.markdown(f"**Backup Alerts ({alert_count})**")
            st.caption(
                "Warning and critical backup issues reported by Datto "
                "in the last 30 calendar days. Saved API data only."
            )
            if not backup_alerts:
                st.success("No explicitly reported backup failures in the saved reporting window.")
            else:
                for i, alert in enumerate(backup_alerts):
                    st.markdown(
                        f"**{html.escape(alert['customer'])}** · "
                        f"{html.escape(alert['application'].removeprefix('Office365'))}"
                    )
                    st.caption(f"{alert['date']} · Datto: {alert['status']}")
                    if st.button("View backup record", key=f"backup_alert_go_{i}",
                                 use_container_width=True):
                        st.session_state["selected_customer_id"] = (
                            int(alert["customer_id"]) if alert["customer_id"].isdigit()
                            else alert["customer_id"]
                        )
                        st.session_state["alert_focus"] = {
                            "customer_id": alert["customer_id"],
                            "suite": alert["suite"], "application": alert["application"],
                            "date": alert["date"],
                        }
                        st.session_state["main_navigation"] = "Customers"
                        st.rerun()
                    if i != len(backup_alerts) - 1:
                        st.divider()
    with refresh_column:
        # One ad-hoc refresh control for the entire application.
        if st.button(
            "↻ Refresh Now",
            key="refresh_now_header",
            disabled=not live_enabled(),
            use_container_width=True,
            help="Requires validated Datto API access" if not live_enabled() else "Collect the latest Datto data",
        ):
            with st.spinner("Collecting Domains, Seats and Applications..."):
                refresh_outcome = run_refresh("manual")
            if refresh_outcome.get("status") == "started":
                st.toast("Background collection started. You can navigate freely.")
            elif refresh_outcome.get("status") == "success":
                st.toast("All three collectors completed successfully.")
            elif refresh_outcome.get("status") == "partial":
                st.warning("Refresh completed with partial failures; check the Log page.")
            else:
                st.warning(refresh_outcome.get("message", "Refresh could not complete."))
            st.rerun()
        if not live_enabled():
            st.caption("Live API collection disabled")

# Read-only UI view of the worker's atomic progress file.
PROGRESS_PATH = ROOT / "data" / "collection_progress.json"


@st.fragment(run_every="2s")
def live_collection_progress():
    """Refresh this small panel independently; do not rerun dashboard or collector."""
    progress = read_json(PROGRESS_PATH, {})
    state = get_state()
    state_status = str(state.get("last_result") or "").lower()
    if state_status != "running":
        # A Streamlit fragment can monitor the separate worker and trigger one
        # full app rerun when a refresh observed as running finishes.
        if state_status in ("success", "partial", "failed"):
            if st.session_state.pop("_observed_background_refresh", False):
                st.rerun(scope="app")
        return

    st.session_state["_observed_background_refresh"] = True

    stage = str(progress.get("stage") or "Starting")
    total = max(0, int(progress.get("total") or 0))
    completed = min(total, max(0, int(progress.get("completed") or 0)))
    name = str(progress.get("customer_name") or "Preparing collection")
    working = bool(progress.get("processing"))
    state_of_progress = str(progress.get("state") or "").lower()
    if state_of_progress in ("success", "partial", "failed"):
        st.info("Background collection has finished. The result will appear in Refresh Status on the next page update.")
        return

    st.markdown("#### Live Collection Progress")
    st.write(f"{stage}: {completed}/{total} customers completed" if total else "Discovering customers…")
    percentage = min(100, max(0, 100 * completed / total if total else 0))
    st.markdown(
        f"""<div style="width:100%;background:#1E293B;height:10px;
                     border-radius:10px;overflow:hidden;">
                <div style="width:{percentage:.2f}%;background:#10B981;
                            height:100%;border-radius:10px;"></div>
            </div>""", unsafe_allow_html=True
    )
    st.caption(f"{'Processing' if working else 'Last processed / stage'}: {name}"
               f" · Seats: {progress.get('seats_completed', 0)}/{total}"
               f" · Applications: {progress.get('applications_completed', 0)}/{total}"
               f" · Failures: {int(progress.get('seats_failed', 0)) + int(progress.get('applications_failed', 0))}")


nav = st.radio(
    "Navigation",
    ["Dashboard", "Customers", "Log", "Settings"],
    horizontal=True,
    label_visibility="collapsed",
    key="main_navigation"
)

# A single 2-second live-progress fragment at the top of the Dashboard.
# It watches the already-running worker and triggers a full rerun on completion.
# It renders nothing when idle; below it, the existing warning/result block applies.
if nav == "Dashboard":
    live_collection_progress()

# Keep the overdue-refresh notice ABOVE Customer Portfolio, where it is visible
# immediately. This is advisory only: no API request is triggered by page load.
if nav == "Dashboard":
    refresh_state = get_state()

    last_result = str(
        refresh_state.get("last_result") or ""
    ).strip().lower()

    last_attempt = refresh_state.get("last_attempt_at")
    last_success = (
        refresh_state.get("last_successful_at")
        or domain_store.get("last_updated")
    )

    # Each new collection attempt gets a distinct notification identity.
    notice_id = f"{last_result}|{last_attempt}"

    dismissed_id = st.session_state.get(
        "dismissed_collection_notice"
    )

    def collection_notice(message, level):
        message_col, close_col = st.columns(
            [12, 1],
            vertical_alignment="center"
        )

        with message_col:
            if level == "failed":
                st.error(message)
            else:
                st.warning(message)

        with close_col:
            if st.button(
                "✕",
                key="dismiss_collection_notice",
                help="Dismiss this collection notification"
            ):
                st.session_state[
                    "dismissed_collection_notice"
                ] = notice_id
                st.rerun()

    if last_result == "failed":

        if dismissed_id != notice_id:
            collection_notice(
                "🔴 **DATA COLLECTION FAILED** — "
                "The latest Datto API refresh was unsuccessful. "
                f"Last attempt: {date_label(last_attempt)}. "
                f"Last successful refresh: {date_label(last_success)}. "
                "Previously saved data is being displayed. "
                "**To investigate:** Log → Refresh Run History → "
                "Inspect refresh → Failed customer details.",
                "failed"
            )

    elif last_result == "partial":

        if dismissed_id != notice_id:
            collection_notice(
                "🟠 **PARTIAL DATA COLLECTION** — "
                "Some Datto API requests failed. "
                "Previously saved data may be displayed "
                "for affected customers. "
                "**To investigate:** Log → Refresh Run History → "
                "Inspect refresh → Failed customer details.",
                "partial"
            )

    elif last_result != "running":

        refresh_due = next_due(
            refresh_state,
            domain_store.get("last_updated")
        )

        if refresh_due is not None:
            try:
                if isinstance(refresh_due, str):
                    refresh_due = datetime.fromisoformat(
                        refresh_due.replace("Z", "+00:00")
                    )

                if refresh_due.tzinfo is None:
                    refresh_due = refresh_due.replace(
                        tzinfo=timezone.utc
                    )

                overdue = (
                    datetime.now(timezone.utc)
                    >= refresh_due.astimezone(timezone.utc)
                )

            except (
                TypeError,
                ValueError,
                OverflowError,
                AttributeError
            ):
                overdue = False

            if overdue and dismissed_id != notice_id:
                collection_notice(
                    "⚠️ **DATA REFRESH OVERDUE** — "
                    "The latest successful collection is past "
                    "its eight-hour refresh interval. "
                    f"Last successful refresh: {date_label(last_success)} "
                    f"({data_age(last_success)} ago). "
                    "Use **↻ Refresh Now** to update the dashboard.",
                    "warning"
                )
st.divider()

try:
    status = load_collection_status()
except (OSError, ValueError, TypeError):
    status = None

records = []
for domain in domain_store.get("domains", []) or []:
    if not isinstance(domain, dict):
        continue
    stats = domain.get("backupStats") or {}
    active, recent = stats.get("activeServicesCount"), stats.get("activeServicesWithRecentBackupCount")
    records.append({
        "Customer ID": domain.get("saasCustomerId"),
        "Customer": domain.get("saasCustomerName") or domain.get("organizationName") or "Unknown Customer",
        "Domain": domain.get("domain") or "Not provided",
        "Product": domain.get("productType") or "Unknown",
        "Seats": domain.get("seatsUsed"),
        "Active Services": active,
        "Recently Backed Up": recent,
        "Coverage": coverage(active, recent)
    })
df = pd.DataFrame(records, columns=["Customer ID", "Customer", "Domain", "Product", "Seats", "Active Services", "Recently Backed Up", "Coverage"])
if not df.empty:
    df["Seats"] = pd.to_numeric(df["Seats"], errors="coerce")
    df["Backup Health"] = df["Coverage"].apply(health)

# Read-only view of recorded collection history; formats vary by collector version.
def history_rows():
    rows = []
    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            rows.append(event)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
    return rows

def collection_summary():
    if not isinstance(status, dict):
        st.info("No automated collection status is available yet. Showing the existing local snapshot.")
        return
    completed = status.get("completed_at")
    domains = status.get("domains") or {}
    seats = status.get("seats") or {}
    apps = status.get("applications") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Last collection", date_label(completed))
    c2.metric("Domains API", str(domains.get("status", "Unknown")))
    c3.metric("Seats", f"{seats.get('successful', 0)} succeeded / {seats.get('failed', 0)} failed")
    c4.metric("Applications", f"{apps.get('successful', 0)} succeeded / {apps.get('failed', 0)} failed")
    st.caption("Recorded collection result, not a live API health check.")

RUN_HISTORY_FILE = ROOT / "data" / "collection_runs.jsonl"


def saved_collection_runs():
    """Read factual previous refresh results, newest first."""
    records = []
    try:
        with RUN_HISTORY_FILE.open(encoding="utf-8") as source:
            for line in source:
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        records.append(entry)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass
    return records[::-1]


def render_collection_failures(summary, key_prefix):
    """Show customer-specific results from the completed collection, if recorded."""
    if not isinstance(summary, dict):
        st.info("No saved collection results available.")
        return
    customer_names = {
        str(r.get("Customer ID")): str(r.get("Customer"))
        for _, r in df.iterrows()
    } if not df.empty else {}
    for category in ("seats", "applications"):
        details = summary.get(category) or {}
        results = details.get("results") or []
        failures = [r for r in results if isinstance(r, dict) and r.get("status") != "success"]
        st.markdown(f"**{category.title()} — {details.get('successful', 0)} succeeded, {details.get('failed', 0)} failed**")
        if failures:
            rows = []
            for item in failures:
                cid = item.get("customer_id")
                rows.append({"Customer": customer_names.get(str(cid), f"Customer {cid}"),
                             "Customer ID": cid, "Result": item.get("status", "failed"),
                             "Error": item.get("error", "Not specified by collector")})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        elif int(details.get("failed") or 0) > 0:
            st.warning("A failure was recorded, but this older run has no customer-level details saved.")
        else:
            st.caption("No failed customer requests.")
    st.caption("A failed request retains its previous successful customer snapshot; the failure above refers to the latest collection attempt.")


def refresh_panel(domain_snapshot):
    """Display factual last-run and next-due information; no fabricated API state."""
    settings = get_settings()
    state = get_state()
    due = next_due(state, domain_snapshot)
    st.subheader("Refresh Status")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Last successful refresh", date_label(state.get("last_successful_at")))
    r2.metric("Last attempt", date_label(state.get("last_attempt_at")))
    r3.metric(
        "Next refresh due",
        (date_label(due.isoformat()) if due else "Awaiting first collection")
        if settings["automatic_refresh"] else "Automatic refresh off",
    )
    result = state.get("last_result", "Not recorded")
    status_colors = {
        "success": "#10B981", "partial": "#F59E0B",
        "failed": "#EF4444", "running": "#60A5FA",
    }
    result_color = status_colors.get(str(result).strip().lower(), "#E8EFF9")
    with r4:
        st.markdown(
            f"""
            <div style="background:#151F31;border:1px solid #28364D;
                        border-radius:14px;padding:19px 22px;min-height:115px;
                        box-sizing:border-box;">
                <div style="color:#A7B6CB;font-size:14px;">Last refresh result</div>
                <div style="color:{result_color};font-size:32px;
                            font-weight:700;margin-top:8px;">
                    {html.escape(str(result).title())}
                </div>
            </div>
            """, unsafe_allow_html=True
        )
    if settings["automatic_refresh"]:
        st.caption("Eight-hour automatic refresh configured. Unattended runs require collection_scheduler.py to be running; this display cannot confirm the separate process is alive.")
    else:
        st.caption("Eight-hour automatic refresh is off. You can enable it in Settings.")
    if lock_present() and str(state.get("last_result", "")).lower() != "running":
        st.warning("A collection lock exists. A refresh may be running, or a previous run may have stopped unexpectedly.")
    if not live_enabled():
        st.info("Live Datto collection is disabled while API compatibility is unverified. Stored snapshots remain available.")


def overdue_check_on_dashboard_open(snapshot):
    """Compatibility no-op: opening the UI must never initiate collection.

    Overdue advice and the explicit manual action are in refresh_panel().
    The separate Windows scheduler continues to handle unattended checks.
    """
    return None


def validate_datto_credentials(public_key, secret_key, base_url):
    """Read-only check; never saves API keys or modifies stored snapshots."""
    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/v1/saas/domains",
            auth=(public_key, secret_key),
            headers={"Accept": "application/json"},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False, f"Datto API returned HTTP {response.status_code}. Check the credentials and base URL."
        # Make sure the endpoint returned the same supported shape as the collector.
        if not isinstance(response.json(), list):
            return False, "Datto returned an unexpected domains response format."
        return True, None
    except (requests.RequestException, ValueError) as error:
        return False, f"Unable to validate Datto credentials ({type(error).__name__}). Check connectivity and configuration."


def store_validated_api_credentials(public_key, secret_key, base_url):
    """Publish a validated set of credentials atomically; restore on audit failure."""
    keys = {
        "DATTO_PUBLIC_KEY": public_key,
        "DATTO_SECRET_KEY": secret_key,
        "DATTO_BASE_URL": base_url,
        "DATTO_ENABLE_LIVE_COLLECTION": "1",
    }
    existed = ENV_FILE.exists()
    old_bytes = ENV_FILE.read_bytes() if existed else None
    old_env = {key: os.environ.get(key) for key in keys}
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".datto-env-", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            if old_bytes is not None:
                f.write(old_bytes)
        for key, value in keys.items():
            set_key(str(tmp), key, value)
        os.replace(tmp, ENV_FILE)
        tmp = None
        os.environ.update(keys)
        record_activity(
            st.session_state["authenticated_windows_user"],
            "API_CREDENTIALS_UPDATED" if old_env.get("DATTO_PUBLIC_KEY") and old_env.get("DATTO_SECRET_KEY") else "API_CREDENTIALS_CONFIGURED",
            details="Datto API credentials validated and saved; secret values not logged",
        )
        return True
    except OSError:
        # Fail closed: restore the prior working configuration if audit or publish failed.
        try:
            if old_bytes is not None:
                with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".datto-rollback-", suffix=".tmp", delete=False) as f:
                    rollback = Path(f.name)
                    f.write(old_bytes)
                os.replace(rollback, ENV_FILE)
            elif ENV_FILE.exists():
                ENV_FILE.unlink()
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        except OSError:
            st.error("CRITICAL: Could not restore prior credentials. Stop collection and contact your administrator.")
        st.error("Credential change was not completed. Check file permissions and audit storage.")
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _clear_pending_api_change():
    st.session_state.pop("pending_api_credentials", None)
    st.session_state.pop("api_credential_stage", None)


@st.dialog("Existing Datto API credentials", dismissible=False)
def confirm_existing_api_keys():
    """Deliberate first acknowledgement before the existing as-is disclaimer."""
    if not st.session_state.get("pending_api_credentials"):
        _clear_pending_api_change()
        st.rerun()
    st.warning(
        "API credentials are already configured. Replacing them may interrupt "
        "future Datto data collection or prevent the dashboard from refreshing. "
        "Existing credentials will be retained if validation fails."
    )
    with st.form("api_replacement_warning_form"):
        accepted = st.checkbox(
            "I understand the risks and agree to replace the existing API credentials."
        )
        left, right = st.columns(2)
        with left:
            cancel = st.form_submit_button("Cancel", use_container_width=True)
        with right:
            proceed = st.form_submit_button("Continue", type="primary", use_container_width=True)
    if cancel:
        _clear_pending_api_change()
        st.rerun()
    if proceed:
        if not accepted:
            st.error("Please acknowledge the risks before continuing.")
            return
        st.session_state["api_credential_stage"] = "disclaimer"
        st.rerun()


@st.dialog("Before you continue", dismissible=False)
def credential_disclaimer():
    """Same as-is disclaimer for initial entry and replacement credentials."""
    pending = st.session_state.get("pending_api_credentials")
    if not pending:
        _clear_pending_api_change()
        st.rerun()
    st.markdown(
        'This tool is provided **“as is”** and **“as available”**, '
        'without warranty of any kind, express or implied. That includes, '
        'without limitation, warranties of accuracy, reliability, availability, '
        'fitness for a particular purpose or non-infringement.'
    )
    st.markdown(
        '**Kaseya provides no support, maintenance or updates for this tool.** '
        'Kaseya accepts no liability for any loss or damage arising from its use.'
    )
    st.markdown(
        'You are responsible for the API keys you enter, for the data this '
        'tool retrieves and stores **locally on this computer**, and for how '
        'you use it. Status shown here may be incomplete or out of date, so '
        'confirm anything important in the Datto portal before acting on it.'
    )
    with st.form("api_disclaimer_form"):
        agreed = st.checkbox(
            'I have read and understand these terms and wish to save my API credentials.'
        )
        left, right = st.columns(2)
        with left:
            cancel = st.form_submit_button("Cancel", use_container_width=True)
        with right:
            confirm = st.form_submit_button("Accept & Save API Keys", type="primary", use_container_width=True)
    if cancel:
        _clear_pending_api_change()
        st.rerun()
    if confirm:
        if not agreed:
            st.error("Accept the disclaimer to continue.")
            return
        if lock_present() or str(get_state().get("last_result") or "").lower() == "running":
            st.error("A collection is running or a collection lock exists. Nothing was changed.")
            return
        public_key, secret_key, base_url = pending
        with st.spinner("Validating Datto API credentials..."):
            valid, error_message = validate_datto_credentials(public_key, secret_key, base_url)
        if not valid:
            st.error(f"Credentials NOT changed. Existing keys retained. {error_message}")
            return
        had_existing = bool(os.getenv("DATTO_PUBLIC_KEY") and os.getenv("DATTO_SECRET_KEY"))
        if store_validated_api_credentials(public_key, secret_key, base_url):
            # Never keep submitted credentials in Streamlit state after successful save.
            _clear_pending_api_change()
            st.session_state["credentials_saved_notice"] = (
                "Replacement API credentials validated and saved. Change recorded in User Activity Audit. "
                "Restart the separate collection scheduler so it loads the new credentials."
                if had_existing else
                "API credentials validated and saved. Configuration recorded in User Activity Audit."
            )
            st.rerun()


if nav == "Dashboard":
    # 1. CUSTOMER PORTFOLIO — preserve search, selection and drill-down.
    st.subheader("Customer Portfolio")
    search = st.text_input("Search customers", placeholder="Search by customer, domain or product...")
    filtered = df.copy()
    if search:
        mask = filtered[["Customer", "Domain", "Product"]].astype(str).apply(lambda col: col.str.contains(search, case=False, regex=False)).any(axis=1)
        filtered = filtered[mask]
    # Shared portfolio/doughnut classifier uses last successfully saved domain
    # recent-backup counts and latest application history. Scheduled refresh
    # cadence does not mean Datto's recent-backup window equals eight hours.
    _, customer_classifications = customer_backup_status_distribution(df, apps_store)
    customer_status_by_id = {
        str(entry["Customer ID"]): entry["Status"]
        for entry in customer_classifications
    }
    customer_reason_by_id = {
        str(entry["Customer ID"]): entry["Reason"]
        for entry in customer_classifications
    }
    filtered["Backup Status"] = filtered["Customer ID"].map(
        lambda cid: "Complete" if customer_status_by_id.get(str(cid)) == "Perfect"
        else customer_status_by_id.get(str(cid), "Unavailable")
    )
    filtered["Classification Reason"] = filtered["Customer ID"].map(
        lambda cid: customer_reason_by_id.get(str(cid), "Data Quality: no matching customer record")
    )
    portfolio_columns = ["Customer", "Domain", "Product", "Seats", "Coverage", "Backup Health", "Backup Status", "Classification Reason"]

    if filtered.empty:
        portfolio = pd.DataFrame(columns=portfolio_columns)
    else:
        portfolio = filtered[portfolio_columns].copy()
    portfolio["Coverage"] = portfolio["Coverage"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "N/A")
    st.caption("Backup Status is calculated from the latest saved Datto snapshot: >9 services without recent "
               "backup = Failed (Custom Rule); 1–9 = Imperfect (Custom Rule). Explicit Datto failures "
               "are labelled separately in Classification Reason. These are service coverage gaps, NOT "
               "failed-item counts or proof of an exact eight-hour outage. Missing data is Unavailable. "
               "A failed collection retains its last saved snapshot; check Refresh Status for data age.")
    selection = st.dataframe(portfolio, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row", key="portfolio_select")
    if selection.selection.rows:
        idx = selection.selection.rows[0]
        if idx < len(filtered):
            st.session_state["selected_customer_id"] = filtered.iloc[idx]["Customer ID"]
            st.success("Customer selected.")


            def open_selected_customer():
                st.session_state["main_navigation"] = "Customers"


            st.button(
                "Open Selected Customer",
                key="open_selected_customer",
                type="primary",
                on_click=open_selected_customer
            )

    # 2. BACKUP STATUS DISTRIBUTION — conservative aggregate API classification.
    render_backup_status_donut(df, apps_store)

    # 3. BACKUP COVERAGE OVERVIEW — unchanged coverage calculation.
    valid = df.dropna(subset=["Coverage", "Active Services", "Recently Backed Up"])
    valid = valid[valid["Active Services"] > 0]
    st.subheader("Backup Coverage Overview")
    if not valid.empty:
        group = valid.groupby("Customer", as_index=False)[["Active Services", "Recently Backed Up"]].sum()
        group["Coverage"] = 100 * group["Recently Backed Up"] / group["Active Services"]
        draw_coverage(group, "Customer", "Recent backup coverage by customer")
    else:
        st.info("No valid coverage statistics in the domain snapshot.")

    # 4. REFRESH STATUS — existing refresh/scheduler behavior unchanged.
    st.caption(f"Last domain snapshot: {date_label(domain_store.get('last_updated'))} · Data age: {data_age(domain_store.get('last_updated'))}")
    refresh_panel(domain_store.get("last_updated"))

    # 5. DATA COLLECTION HEALTH — existing diagnostics unchanged.
    st.subheader("Data Collection Health")
    collection_summary()
    current_summary = get_state().get("last_summary") or status
    if isinstance(current_summary, dict):
        failure_count = sum(int((current_summary.get(k) or {}).get("failed") or 0)
                            for k in ("seats", "applications"))
        with st.expander(f"View collection details · {failure_count} failed request(s)",
                         expanded=failure_count > 0):
            render_collection_failures(current_summary, "dashboard")
            st.caption(f"Collection finished: {date_label(current_summary.get('completed_at'))}")
    st.info(f"Backup coverage available for {valid['Customer ID'].nunique()} of {df['Customer ID'].nunique()} customers. Unavailable data is not counted as failed backups.")


elif nav == "Customers":
    st.subheader("Customer Intelligence")
    if df.empty:
        st.info("No customers in the local domain snapshot.")
    else:
        customer_options = df[["Customer ID", "Customer"]].drop_duplicates(subset=["Customer ID"])
        ids = customer_options["Customer ID"].tolist()
        current = st.session_state.get("selected_customer_id")
        selected = st.selectbox("Customer", ids, index=ids.index(current) if current in ids else 0,
                                format_func=lambda cid: customer_options.loc[customer_options["Customer ID"] == cid, "Customer"].iloc[0])
        st.session_state["selected_customer_id"] = selected
        rows = df[df["Customer ID"] == selected]
        valid = rows.dropna(subset=["Coverage", "Active Services", "Recently Backed Up"])
        active, recent = valid["Active Services"].sum(), valid["Recently Backed Up"].sum()
        cust_coverage = 100 * recent / active if active > 0 else None
        seat_records, seat_updated = get_seats(seats_store, selected)
        pages, app_updated = get_apps(apps_store, selected)
        apps = application_rows(pages)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active Services", f"{int(active):,}" if active > 0 else "N/A")
        c2.metric("Recently Backed Up", f"{int(recent):,}" if active > 0 else "N/A")
        c3.metric("Recent Backup Coverage", f"{cust_coverage:.1f}%" if cust_coverage is not None else "N/A")
        c4.metric("Backup Health", health(cust_coverage))
        st.caption(f"Domain snapshot: {date_label(domain_store.get('last_updated'))} · Seats: {date_label(seat_updated)} · Applications: {date_label(app_updated)}")
        focus = st.session_state.get("alert_focus")
        if isinstance(focus, dict) and str(focus.get("customer_id")) == str(selected):
            match = daily_application_history(pages, days=30).get(
                (focus.get("suite"), focus.get("application")), {}
            )
            try:
                focus_date = datetime.fromisoformat(focus["date"]).date()
            except (TypeError, ValueError, KeyError):
                focus_date = None
            observation = match.get(focus_date)
            if observation is not None:
                st.error(
                    "🔔 Backup failure · "
                    f"{focus['application'].removeprefix('Office365')} · "
                    f"{focus['date']} · Datto status: {observation['raw_status']}"
                )
                st.markdown("**Selected backup record (saved Datto API observation)**")
                st.json(observation.get("source_window") or {}, expanded=False)
            else:
                st.warning("Selected alert record is no longer present in the current saved snapshot.")
            resolution_candidate = next(
                (alert for alert in backup_alerts
                 if str(alert["customer_id"]) == str(focus.get("customer_id"))
                 and alert["suite"] == focus.get("suite")
                 and alert["application"] == focus.get("application")
                 and alert["date"] == focus.get("date")),
                None,
            )
            action_col, close_col = st.columns([1, 1])
            with action_col:
                if st.button(
                        "✅ Mark Resolved",
                        key="mark_backup_alert_resolved",
                        disabled=resolution_candidate is None,
                        help="Confirm the specific issue was resolved and remove it from outstanding alerts."
                ):
                    st.session_state["pending_resolution_alert"] = dict(resolution_candidate)
                    st.rerun()
            with close_col:
                if st.button("Close backup alert details", key="clear_alert_focus"):
                    try:
                        record_activity(
                            st.session_state["authenticated_windows_user"],
                            "ALERT_DETAILS_CLOSED",
                            target=f"{rows.iloc[0]['Customer']} · {focus.get('application', '')}",
                            alert_id=json.dumps([str(focus.get("customer_id", "")), str(focus.get("suite", "")),
                                                 str(focus.get("application", "")), str(focus.get("date", ""))],
                                                separators=(",", ":")),
                            details="Closed backup alert details without resolving the alert",
                        )
                    except OSError:
                        st.error("Unable to record activity; the alert details remain open.")
                        st.stop()
                    st.session_state.pop("alert_focus", None)
                    st.session_state.pop("pending_resolution_alert", None)
                    st.rerun()
        summary, backup_tab, seats_tab, applications_tab = st.tabs(["Customer Summary", "Backup Health", "Seat Intelligence", "Application Intelligence"])
        with summary:
            st.markdown("#### Customer Protection Summary")
            st.dataframe(rows[["Domain", "Product", "Seats", "Coverage", "Backup Health"]], hide_index=True, use_container_width=True)

            # Generate a PDF containing only the selected customer's data
            pdf_customer_name = customer_options.loc[
                customer_options["Customer ID"] == selected,
                "Customer"
            ].iloc[0]

            pdf_bytes = generate_customer_pdf(
                customer_rows=rows,
                seat_records=seat_records,
                application_records=apps if pages is not None else None,
                history_records=None,
                customer_name=pdf_customer_name,
                snapshot_at=domain_store.get("last_updated")
            )

            st.download_button(
                label="↓ Download Customer Protection Report (PDF)",
                data=pdf_bytes,
                file_name=f"Customer_Protection_Report_{selected}.pdf",
                mime="application/pdf",
                key="download_customer_pdf"
            )


            s1, s2, s3 = st.columns(3)
            s1.metric("Domains", len(rows))
            s2.metric("Seats retrieved", len(seat_records) if seat_records is not None else "N/A")
            s3.metric("Application history rows", len(apps) if pages is not None else "N/A")
            if seat_records is None or pages is None:
                st.info("Some customer intelligence is unavailable until the Datto APIs return data.")
        with backup_tab:
            st.caption("Recent backup coverage for the domains associated with this customer.")
            st.dataframe(rows[["Domain", "Product", "Seats", "Active Services", "Recently Backed Up", "Coverage", "Backup Health"]],
                         hide_index=True, use_container_width=True,
                         column_config={"Coverage": st.column_config.ProgressColumn("Backup Coverage", min_value=0, max_value=100, format="%.1f%%")})

        with seats_tab:
            st.caption("Customer seat inventory, distribution and billing visibility.")
            if seat_records is None:
                st.info("Seat inventory has not been collected for this customer yet.")
            elif not seat_records:
                st.info("The latest successful collection returned zero seats.")
            else:
                seat_df = pd.DataFrame([x for x in seat_records if isinstance(x, dict)])
                st.metric("Seats Retrieved", len(seat_df))
                if "seatType" in seat_df:
                    counts = seat_df["seatType"].fillna("Unknown").value_counts()
                    fig = go.Figure(go.Pie(labels=counts.index.tolist(), values=counts.values.tolist(), hole=.6))
                    fig.update_layout(template="plotly_dark", paper_bgcolor="#0B1120", height=360, margin=dict(l=20, r=20, t=20, b=20))
                    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
                def billing_status(value):
                    if pd.isna(value) or str(value).strip() == "":
                        return "Unknown"
                    text = str(value).strip().lower()
                    if text in ("1", "true", "yes"):
                        return "Billable"
                    if text in ("0", "false", "no"):
                        return "Not Billable"
                    return "Unknown"

                if "billable" in seat_df:
                    billing = seat_df["billable"].apply(billing_status)
                    b1, b2 = st.columns(2)
                    b1.metric("Billable Seats", int((billing == "Billable").sum()))
                    b2.metric("Non-Billable Seats", int((billing == "Not Billable").sum()))
                preferred = [x for x in ["name", "mainId", "seatType", "seatState", "billable", "dateAdded"] if x in seat_df]
                display_df = seat_df[preferred].copy() if preferred else seat_df.copy()
                if "billable" in display_df:
                    display_df["_sort_billable"] = display_df["billable"].apply(billing_status).eq("Billable")
                    display_df = display_df.sort_values("_sort_billable", ascending=False, kind="stable").drop(columns="_sort_billable")
                st.markdown("#### Filter Seat Records")
                f1, f2, f3 = st.columns(3)
                with f1:
                    states = sorted(display_df["seatState"].dropna().astype(str).unique()) if "seatState" in display_df else []
                    selected_states = st.multiselect("Seat State", states, default=states, key=f"seat_states_{selected}")
                with f2:
                    types = sorted(display_df["seatType"].dropna().astype(str).unique()) if "seatType" in display_df else []
                    selected_types = st.multiselect("Seat Type", types, default=types, key=f"seat_types_{selected}")
                with f3:
                    selected_billing = st.multiselect("Billing Status", ["Billable", "Not Billable", "Unknown"],
                                                    default=["Billable", "Not Billable", "Unknown"], key=f"seat_billing_{selected}")
                filtered_df = display_df.copy()
                if "seatState" in filtered_df:
                    filtered_df = filtered_df[filtered_df["seatState"].astype(str).isin(selected_states)]
                if "seatType" in filtered_df:
                    filtered_df = filtered_df[filtered_df["seatType"].astype(str).isin(selected_types)]
                if "billable" in filtered_df:
                    filtered_df = filtered_df[filtered_df["billable"].apply(billing_status).isin(selected_billing)]
                filtered_df.insert(0, "Sr. No.", range(1, len(filtered_df) + 1))
                st.caption(f"Showing {len(filtered_df)} of {len(display_df)} seat records")
                st.dataframe(filtered_df, hide_index=True, width="stretch")

        with applications_tab:
            st.markdown("#### 90-Day Application Backup History")
            st.caption("Daily statuses are derived from actual timestamped Datto backup windows in the saved application snapshot.")
            show_placeholders = rows["Product"].astype(str).str.contains("Office365|Microsoft365|M365", case=False, regex=True).any()
            dated_observations = render_application_timeline(
                pages, days=90, show_office365_placeholders=show_placeholders
            )
            if dated_observations:
                st.download_button(
                    "↓ Download 90-Day Application History (PDF)",
                    data=make_history_pdf(
                        pdf_customer_name, dated_observations,
                        seat_records=seat_records,
                        outstanding_alerts=[a for a in backup_alerts
                                            if str(a.get("customer_id")) == str(selected)],
                    ),
                    file_name=f"Application_History_90_Days_{selected}.pdf",
                    mime="application/pdf",
                    key="download_history_pdf",
                )
            if pages is not None and apps:
                application_drilldown(pages, selected)

elif nav == "Log":
    st.subheader("User Activity Audit")
    st.caption("Windows-authenticated actions, most recent first. Times shown in the computer's local time zone.")
    try:
        activity = read_activity()
        if activity:
            activity_rows = [{
                "Local Date / Time": (datetime.fromisoformat(event["timestamp_utc"]).astimezone()
                                       .strftime("%d/%m/%Y %I:%M:%S %p %Z")),
                "Windows User": event.get("actor", ""),
                "Action": event.get("action", ""),
                "Customer / Application": event.get("target", ""),
                "Alert ID": event.get("alert_id", ""),
                "Details": event.get("details", ""),
                "Audit Timestamp (Local)": (
                    datetime.fromisoformat(event["timestamp_utc"]).astimezone().strftime("%d/%m/%Y %I:%M:%S %p")
                    if event.get("timestamp_utc") else ""
                ),
            } for event in reversed(activity)]
            st.dataframe(pd.DataFrame(activity_rows), hide_index=True, use_container_width=True)
        else:
            st.info("No user actions recorded yet.")
    except (OSError, ValueError, KeyError, TypeError):
        st.error("Unable to read the user activity audit trail. Please check the audit file.")
    st.divider()
    st.subheader("Collection Log")
    st.caption("Actual locally recorded events only. No synthetic daily status records are generated.")
    events = history_rows()
    st.markdown("#### Stored history")
    if events:
        st.caption("These are raw storage-history events; they may not represent daily application backup results.")
        history_df = pd.json_normalize(events, sep=".")
        query = st.text_input("Filter history", placeholder="Search stored events...")
        if query:
            history_df = history_df[history_df.astype(str).apply(lambda c: c.str.contains(query, case=False, regex=False)).any(axis=1)]
        st.dataframe(history_df.tail(500).iloc[::-1], use_container_width=True, hide_index=True)
        export_button("↓ Export History CSV", history_df, "datto_storage_history.csv", "export_history")
    else:
        st.info("No local history events are available.")
    st.markdown("#### Refresh Run History")
    runs = saved_collection_runs()
    if runs:
        st.caption(f"{len(runs)} completed refresh run(s) recorded. Latest first. "
                   "Earlier runs are shown only if their history was saved.")
        overview = []
        for entry in runs:
            seats_info = entry.get("seats") or {}
            apps_info = entry.get("applications") or {}
            seat_failures = int(seats_info.get("failed") or 0)
            app_failures = int(apps_info.get("failed") or 0)
            domain_ok = str((entry.get("domains") or {}).get("status", "")).lower() in (
                "updated", "unchanged", "success", "no_change", "no_changes")
            total_failures = seat_failures + app_failures
            outcome = "Success" if domain_ok and total_failures == 0 else "Partial"
            overview.append({
                "Completed": date_label(entry.get("completed_at")),
                "Result": outcome,
                "Customers": entry.get("customers"),
                "Seats Failed": seat_failures,
                "Applications Failed": app_failures,
            })
        st.dataframe(pd.DataFrame(overview), hide_index=True, width="stretch")
        chosen_index = st.selectbox(
            "Inspect refresh", list(range(len(runs))),
            format_func=lambda i: (
                f"{date_label(runs[i].get('completed_at'))} · "
                f"{overview[i]['Result']} · "
                f"{overview[i]['Seats Failed'] + overview[i]['Applications Failed']} failed"
            ), key="historic_refresh_selection"
        )
        render_collection_failures(runs[chosen_index], "log")
    else:
        st.caption("No completed refresh history has been recorded yet.")
    st.markdown("#### Latest collection summary")
    if isinstance(status, dict):
        st.json(status, expanded=False)
        st.download_button("↓ Export Collection Status JSON", json.dumps(status, indent=2), file_name="datto_collection_status.json", mime="application/json")
    else:
        st.info("No automated collection run has been recorded.")

    st.divider()
    st.markdown("#### Raw API Data — Applications")
    st.caption("Technical diagnostics only. Displays saved API snapshots; no live requests are made.")
    diagnostic_customers = df[["Customer ID", "Customer"]].drop_duplicates(subset=["Customer ID"]) if not df.empty else pd.DataFrame()
    if diagnostic_customers.empty:
        st.info("No customers are available in the domain snapshot.")
    else:
        diagnostic_ids = diagnostic_customers["Customer ID"].tolist()

        # Use the same selected customer across Dashboard, Customers and Log.
        # A keyed Streamlit selectbox retains its previous value unless we sync it.
        shared_customer = st.session_state.get("selected_customer_id")
        if shared_customer not in diagnostic_ids:
            shared_customer = diagnostic_ids[0]
            st.session_state["selected_customer_id"] = shared_customer
        if st.session_state.get("log_raw_customer") != shared_customer:
            st.session_state["log_raw_customer"] = shared_customer

        def sync_log_customer():
            st.session_state["selected_customer_id"] = st.session_state["log_raw_customer"]
            # A different customer may have a different set of apps and dates.
            st.session_state.pop("log_raw_application", None)
            st.session_state.pop("log_raw_backup_date", None)

        diagnostic_customer = st.selectbox(
            "Customer", diagnostic_ids,
            format_func=lambda cid: diagnostic_customers.loc[
                diagnostic_customers["Customer ID"] == cid, "Customer"
            ].iloc[0],
            key="log_raw_customer",
            on_change=sync_log_customer,
        )
        diagnostic_pages, diagnostic_updated = get_apps(apps_store, diagnostic_customer)
        st.caption(f"Saved applications snapshot: {date_label(diagnostic_updated)}")
        if diagnostic_pages is None:
            st.info("No saved Applications API response for this customer.")
        else:
            with st.expander("Complete Applications API response"):
                st.json(diagnostic_pages, expanded=False)

            diagnostic_history = daily_application_history(diagnostic_pages, days=90)
            diagnostic_keys = sorted(diagnostic_history, key=lambda k: (k[0], k[1]))
            diagnostic_keys = [k for k in diagnostic_keys if diagnostic_history[k]]
            if diagnostic_keys:
                diagnostic_app = st.selectbox(
                    "Application", diagnostic_keys,
                    format_func=lambda k: f"{k[1].removeprefix('Office365')} ({k[0]})",
                    key="log_raw_application",
                )
                diagnostic_records = sorted(
                    diagnostic_history[diagnostic_app].values(),
                    key=lambda r: r["date"], reverse=True,
                )
                diagnostic_date = st.selectbox(
                    "Backup date", [r["date"] for r in diagnostic_records],
                    key="log_raw_backup_date",
                )
                chosen_window = next(
                    r["source_window"] for r in diagnostic_records
                    if r["date"] == diagnostic_date
                )
                with st.expander("Full backup window returned by Datto", expanded=True):
                    st.json(chosen_window, expanded=True)
            else:
                st.info("No timestamped backup windows are present in the saved snapshot.")

elif nav == "Settings":
    st.subheader("Settings")
    st.caption("API credentials, refresh controls and local branding. Credentials are never displayed after saving.")
    st.markdown("#### Datto API Credentials")
    st.markdown(
        'Public key: <span style="color:#10B981;font-weight:600;">Configured</span>'
        if os.getenv("DATTO_PUBLIC_KEY")
        else 'Public key: <span style="color:#EF4444;font-weight:600;">Missing</span>',
        unsafe_allow_html=True,
    )
    st.markdown(
        'Secret key: <span style="color:#10B981;font-weight:600;">Configured</span>'
        if os.getenv("DATTO_SECRET_KEY")
        else 'Secret key: <span style="color:#EF4444;font-weight:600;">Missing</span>',
        unsafe_allow_html=True,
    )
    # Existing keys remain hidden, but replacements are permitted after two
    # explicit confirmations and successful Datto validation.
    already_configured = bool(os.getenv("DATTO_PUBLIC_KEY") and os.getenv("DATTO_SECRET_KEY"))
    collection_busy = (lock_present() or str(get_state().get("last_result") or "").lower() == "running")

    with st.form("credentials_form"):
        public_key = st.text_input("New public key", type="password")
        secret_key = st.text_input("New secret key", type="password")
        base_url = st.text_input(
            "Datto API base URL", value=os.getenv("DATTO_BASE_URL", "https://api.datto.com")
        )
        save_keys = st.form_submit_button(
            "Save API configuration", disabled=collection_busy
        )

    if already_configured:
        st.info("API credentials are configured. To replace them, enter new keys and select Save API configuration.")
    if collection_busy:
        st.warning("Credential updates are unavailable while a refresh is active or a collection lock exists.")

    saved_notice = st.session_state.pop("credentials_saved_notice", None)
    if saved_notice:
        st.success(saved_notice)

    if save_keys:
        if collection_busy:
            st.error("Collection in progress. No credentials were changed.")
        elif not public_key or not secret_key or not base_url.startswith("https://"):
            st.error("Enter both keys and a valid HTTPS base URL.")
        else:
            st.session_state["pending_api_credentials"] = (public_key, secret_key, base_url)
            st.session_state["api_credential_stage"] = (
                "existing_warning" if already_configured else "disclaimer"
            )
            st.rerun()

    # Existing behaviour: clients cannot accidentally erase configured API
    # keys through Settings; replacement goes through the confirmed flow.
    if st.button("Forget saved API keys", disabled=already_configured or collection_busy):
        if ENV_FILE.exists():
            unset_key(str(ENV_FILE), "DATTO_PUBLIC_KEY")
            unset_key(str(ENV_FILE), "DATTO_SECRET_KEY")
        os.environ.pop("DATTO_PUBLIC_KEY", None)
        os.environ.pop("DATTO_SECRET_KEY", None)
        st.success("Saved API keys removed.")
    st.divider()
    st.markdown("#### Automatic Refresh")
    current_refresh_settings = get_settings()
    with st.form("refresh_settings_form"):
        automatic = st.checkbox(
            "Automatic refresh every 8 hours",
            value=current_refresh_settings["automatic_refresh"],
        )
        overdue_on_open = st.checkbox(
            "Show overdue warning when dashboard opens (manual refresh only)",
            value=current_refresh_settings["check_overdue_on_open"],
        )
        if st.form_submit_button("Save refresh settings"):
            save_settings(automatic, overdue_on_open)
            st.success("Refresh preferences saved locally.")
    st.caption("Manual Refresh Now remains available when live collection is explicitly enabled, regardless of automatic scheduling.")
    st.write("Live API collection gate:", "Enabled" if live_enabled() else "Disabled")
    st.info("Local automatic collection is managed by collection_scheduler.py; the scheduler must be running independently.")
    st.caption("Live Datto collection is controlled by DATTO_ENABLE_LIVE_COLLECTION.")
    st.divider()
    st.markdown("#### Dashboard Branding")
    with st.form("branding_form"):
        company_name = st.text_input("Dashboard name", value=str(brand.get("company_name", "")))
        subtitle = st.text_input("Subtitle", value=str(brand.get("subtitle", "")))
        logo_path = st.text_input("Logo filename (place image beside dashboard.py)", value=str(brand.get("logo", "")))
        if st.form_submit_button("Save branding"):
            safe_logo = Path(logo_path).name if logo_path else ""
            BRAND_FILE.write_text(json.dumps({"company_name": company_name, "subtitle": subtitle, "logo": safe_logo}, indent=2), encoding="utf-8")
            st.success("Branding saved. Refresh the page to see changes.")
    st.divider()
    st.markdown("#### History Backup")
    if HISTORY_FILE.exists():
        st.download_button("↓ Export Local History (JSONL)", HISTORY_FILE.read_bytes(), file_name="datto_history.jsonl", mime="application/x-ndjson")
    else:
        st.caption("No history file is present.")
    st.caption("Data files remain on this computer. This dashboard is intended for trusted local use, not unauthenticated public hosting.")

# Keep the alert-resolution dialog open through unrelated full-app reruns
# (including activity events from the authenticated idle-session component).
# The actual resolution is written only on explicit form submission.
pending_alert = st.session_state.get("pending_resolution_alert")
if isinstance(pending_alert, dict) and st.session_state.get("authenticated_lease_token"):
    confirm_backup_resolution(pending_alert)

st.divider()
st.caption("SaaS Protection Intelligence · Local reporting dashboard · Datto REST API snapshots · Provided as-is")



# Keep the staged credential confirmation dialog open across unrelated full
# Streamlit reruns (including activity events from the idle-session component).
# Never open both kinds of modal on the same run.
if (st.session_state.get("authenticated_lease_token")
        and not st.session_state.get("pending_resolution_alert")):
    credential_stage = st.session_state.get("api_credential_stage")
    if credential_stage == "existing_warning":
        confirm_existing_api_keys()
    elif credential_stage == "disclaimer":
        credential_disclaimer()
