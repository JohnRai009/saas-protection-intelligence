"""Customer-specific Datto SaaS Protection PDF report.

Generate a PDF in memory with actual supplied customer data only.
No API requests or on-disk writes are performed.

Usage from Streamlit:
    pdf_bytes = generate_customer_pdf(
        customer_rows=rows,
        seat_records=seat_records,
        application_records=apps,
        customer_name=customer_name,
        snapshot_at=domain_store.get('last_updated'),
        history_records=None,  # supply recorded daily history when available
    )
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor('#102033')
BLUE = colors.HexColor('#155C9A')
LIGHT = colors.HexColor('#F2F6FA')
MUTED = colors.HexColor('#526577')
LINE = colors.HexColor('#D8E2EA')
GREEN = colors.HexColor('#087A54')
AMBER = colors.HexColor('#B97713')
RED = colors.HexColor('#B73535')


def _rows(obj: Any) -> list[dict]:
    """Accept a DataFrame, a list of dictionaries or None."""
    if obj is None:
        return []
    if hasattr(obj, 'to_dict'):
        return obj.to_dict(orient='records')
    if isinstance(obj, list):
        return [dict(item) for item in obj if isinstance(item, dict)]
    raise TypeError('Expected a pandas DataFrame, list of dictionaries, or None')


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        if result != result or result in (float('inf'), float('-inf')):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _text(value: Any, default: str = 'Not available') -> str:
    if value is None or value == '':
        return default
    try:
        if value != value:
            return default
    except (ValueError, TypeError):
        pass
    return str(value)


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).date()
        except ValueError:
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
    return None


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(_text(value)).replace('\n', '<br/>'), style)


def generate_customer_pdf(
    customer_rows: Any,
    seat_records: Any = None,
    application_records: Any = None,
    history_records: Any = None,
    customer_name: str | None = None,
    snapshot_at: str | None = None,
    branding: dict | None = None,
) -> bytes:
    """Return one customer's protection report as downloadable PDF bytes.

    customer_rows: selected customer's domain rows (DataFrame or dict list).
    seat_records: selected customer's seat dict list, or None if unavailable.
    application_records: selected customer's parsed application rows, or None.
    history_records: actual daily history dicts; optional. Expected keys include
        date/day, application, status, services_backed_up, coverage. Records
        outside the past 30 calendar days are excluded. Missing days remain
        missing (never synthesized).
    branding: {'name': ..., 'subtitle': ...}. No company logo is required.
    """
    domains = _rows(customer_rows)
    seats = _rows(seat_records)
    applications = _rows(application_records)
    history = _rows(history_records)
    if not domains:
        raise ValueError('Cannot create a customer PDF without customer domain records')

    # Guard against inadvertently producing an MSP-wide multi-customer report.
    ids = {str(row.get('Customer ID')) for row in domains if row.get('Customer ID') is not None}
    if len(ids) > 1:
        raise ValueError('Customer PDF must contain exactly one SaaS customer ID')

    name = customer_name or _text(domains[0].get('Customer'), 'Selected customer')
    brand = branding or {}
    report_title = _text(brand.get('name'), 'SaaS Protection Intelligence')
    subtitle = _text(brand.get('subtitle'), 'Customer protection and backup reporting')
    now = datetime.now(timezone.utc)

    body = getSampleStyleSheet()
    body.add(ParagraphStyle(name='TitleCustom', parent=body['Title'], fontName='Helvetica-Bold', fontSize=20, leading=25, textColor=NAVY, alignment=TA_LEFT, spaceAfter=7))
    body.add(ParagraphStyle(name='SectionCustom', parent=body['Heading2'], fontName='Helvetica-Bold', fontSize=12, leading=16, textColor=BLUE, spaceBefore=14, spaceAfter=7))
    body.add(ParagraphStyle(name='BodyCustom', parent=body['BodyText'], fontSize=8.5, leading=12.5, textColor=NAVY, spaceAfter=7))
    body.add(ParagraphStyle(name='SmallCustom', parent=body['BodyText'], fontSize=7.5, leading=10, textColor=MUTED, spaceAfter=4))
    body.add(ParagraphStyle(name='CellCustom', parent=body['BodyText'], fontSize=7, leading=9, textColor=NAVY))
    body.add(ParagraphStyle(name='HeaderCellCustom', parent=body['BodyText'], fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.white))

    buffer = BytesIO()
    width, height = (595.28, 841.89)  # A4 points
    left = right = 42
    doc = BaseDocTemplate(buffer, pagesize=(width, height), leftMargin=left, rightMargin=right,
                          topMargin=55, bottomMargin=55, title=f'{name} - Customer Protection Report',
                          author='SaaS Protection Intelligence')
    frame = Frame(left, 55, width - left - right, height - 110, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    def on_page(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(left, height - 37, width - right, height - 37)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(left, height - 29, report_title[:85])
        canvas.line(left, 43, width - right, 43)
        canvas.drawString(left, 30, 'Customer-specific report | Data is based on available stored snapshots')
        canvas.drawRightString(width-right, 30, f'Page {document.page}')
        canvas.restoreState()

    doc.addPageTemplates(PageTemplate(id='Report', frames=[frame], onPage=on_page))
    story = []
    story.append(_paragraph('Customer Protection Report', body['TitleCustom']))
    story.append(_paragraph(f'{name}  |  {subtitle}', body['BodyCustom']))
    story.append(_paragraph(f'Generated (UTC): {now:%d %b %Y %H:%M}   |   Domain snapshot: {_text(snapshot_at)}', body['SmallCustom']))

    def section(title):
        story.append(_paragraph(title, body['SectionCustom']))

    def table(headers, data, widths=None, max_rows=250):
        if not data:
            story.append(_paragraph('Not available - no records have been collected for this section.', body['SmallCustom']))
            return
        if len(data) > max_rows:
            data = data[:max_rows]
            story.append(_paragraph(f'Displaying first {max_rows} records. Additional entries omitted from this report.', body['SmallCustom']))
        contents = [[_paragraph(h, body['HeaderCellCustom']) for h in headers]]
        for row in data:
            contents.append([_paragraph(value, body['CellCustom']) for value in row])
        col_widths = widths or [(width - left - right) / len(headers)] * len(headers)
        t = Table(contents, colWidths=col_widths, repeatRows=1, hAlign='LEFT')
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), NAVY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT]),
            ('GRID', (0, 0), (-1, -1), 0.35, LINE),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 7),
            ('RIGHTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(t)

    total_seats_used = sum((_number(r.get('Seats')) or 0) for r in domains)
    valid = []
    for r in domains:
        active = _number(r.get('Active Services'))
        recent = _number(r.get('Recently Backed Up'))
        if active is not None and recent is not None and active > 0 and 0 <= recent <= active:
            valid.append((active, recent))
    active_sum = sum(a for a, _ in valid)
    recent_sum = sum(v for _, v in valid)
    coverage = (100 * recent_sum / active_sum) if active_sum > 0 else None
    health = ('Unavailable' if coverage is None else 'Healthy' if coverage >= 100 else 'Attention' if coverage >= 90 else 'At risk')

    section('1. Executive Summary')
    table(['Measure', 'Value'], [
        ('Customer', name),
        ('Customer ID', next(iter(ids), 'Not available')),
        ('Domains in snapshot', str(len(domains))),
        ('Seats used (domain snapshot)', f'{int(total_seats_used):,}'),
        ('Active services with valid coverage', f'{int(active_sum):,}' if valid else 'Not available'),
        ('Recently backed up', f'{int(recent_sum):,}' if valid else 'Not available'),
        ('Recent backup coverage', f'{coverage:.1f}%' if coverage is not None else 'Not available'),
        ('Backup health (presentation classification)', health),
    ], [215, 296])

    section('2. Backup Health')
    backup_table = []
    for r in domains:
        a = _number(r.get('Active Services'))
        b = _number(r.get('Recently Backed Up'))
        p = 100 * b/a if a and b is not None and 0 <= b <= a else None
        backup_table.append((r.get('Domain'), r.get('Product'),
                             str(int(a)) if a is not None else 'N/A',
                             str(int(b)) if b is not None else 'N/A',
                             f'{p:.1f}%' if p is not None else 'N/A'))
    table(['Domain', 'Product', 'Active', 'Recent', 'Coverage'], backup_table, [145, 123, 67, 70, 106])
    story.append(Spacer(1, 6))
    story.append(_paragraph('Coverage measures active services with a recent backup. It does not confirm that every individual backup job succeeded.', body['SmallCustom']))

    section('3. Seat Intelligence')
    if seat_records is None:
        story.append(_paragraph('Seat inventory not available. Only the aggregate Seats Used from the domain snapshot is known.', body['BodyCustom']))
    elif not seats:
        story.append(_paragraph('The collected seat inventory contains zero records.', body['BodyCustom']))
    else:
        counts = Counter(_text(r.get('seatType'), 'Unknown') for r in seats)
        table(['Seat type', 'Count'], sorted([(key, str(value)) for key, value in counts.items()]), [330, 181])
        billable_true = sum(r.get('billable') is True for r in seats)
        billable_false = sum(r.get('billable') is False for r in seats)
        story.append(_paragraph(f'Seats retrieved: {len(seats)}  |  Billable: {billable_true}  |  Non-billable: {billable_false}  |  Billing status unknown: {len(seats)-billable_true-billable_false}', body['SmallCustom']))
        table(['Seat name', 'Type', 'State', 'Billable'], [
            (r.get('name'), r.get('seatType'), r.get('seatState'),
             'Yes' if r.get('billable') is True else 'No' if r.get('billable') is False else 'Unknown')
            for r in seats
        ], [210, 125, 103, 73])

    section('4. Application Intelligence')
    if application_records is None:
        story.append(_paragraph('Application-level backup information has not been collected for this customer.', body['BodyCustom']))
    elif not applications:
        story.append(_paragraph('No application backup-history records are available.', body['BodyCustom']))
    else:
        app_table = []
        for r in applications:
            pct = _number(r.get('Coverage'))
            app_table.append((r.get('Application'), r.get('Suite'), r.get('Window'),
                              r.get('Status'), f'{pct:.1f}%' if pct is not None else 'N/A'))
        table(['Application', 'Suite', 'Window', 'Status', 'Coverage'], app_table, [115, 104, 110, 95, 87])
        story.append(_paragraph('Different backup-history windows may overlap. They are listed separately and are not added together.', body['SmallCustom']))

    section('5. Historical Analysis - Last 30 Days')
    cutoff = now.date() - timedelta(days=29)
    dated = []
    for r in history:
        day = _date(r.get('date') or r.get('day') or r.get('timestamp'))
        if day and cutoff <= day <= now.date():
            dated.append((day, r))
    dated.sort(key=lambda item: item[0], reverse=True)
    if dated:
        table(['Date', 'Application', 'Status', 'Services backed up', 'Coverage'], [
            (day.isoformat(), r.get('application') or r.get('Application'),
             r.get('status') or r.get('Status'),
             r.get('services_backed_up') or r.get('Services Backed Up'),
             r.get('coverage') or r.get('Coverage')) for day, r in dated
        ], [91, 125, 93, 119, 83], max_rows=200)
        story.append(_paragraph(f'Historical records shown: {len(dated)}. Only days with recorded results are displayed; missing days are not interpreted as successful or failed backups.', body['SmallCustom']))
    else:
        story.append(_paragraph('30-day daily history is not available in the supplied local data. It will appear when actual dated backup records are collected and retained; no historical results have been inferred.', body['BodyCustom']))

    section('6. Reporting Notes')
    story.append(_paragraph('This report is generated from the selected customer\'s available Datto SaaS Protection data and local snapshots. Values may be delayed or incomplete during API outages. Verify important backup status directly in the Datto portal.', body['BodyCustom']))
    story.append(_paragraph('Provided as-is for informational use; no guarantee is made that the report constitutes a complete record of all backups. Support, warranty and liability terms are governed by the distributor\'s approved handover agreement.', body['SmallCustom']))

    doc.build(story)
    return buffer.getvalue()


if __name__ == '__main__':
    print('Customer PDF report module initialised. No report generated.')
