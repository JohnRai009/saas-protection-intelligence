# SaaS Protection Intelligence
## Windows Client Release V3
**Deployment, Installation & Operational Guide**

**Release:** Version 1  
**Platform:** Microsoft Windows  
**Deployment Model:** Local, self-hosted application  
**Application URL:** http://127.0.0.1:8501  
**Classification:** Client Deployment Documentation

---

## 1. Executive Overview

**SaaS Protection Intelligence** is a locally deployed reporting and monitoring dashboard designed to provide enhanced visibility into Datto SaaS Protection environments.

The solution consolidates customer portfolios, protected user seats, application backup activity, backup health classifications, historical reporting and operational alerts into a unified interface.

The dashboard combines **Datto-reported backup information** with an additional **custom classification layer** to help identify potential protection gaps requiring further investigation.

## Independent Development, Ownership & Support Disclaimer

**SaaS Protection Intelligence is an independently developed utility and is not an official Kaseya or Datto product.**

This application has been developed independently to provide supplementary reporting, visibility and operational intelligence using information obtained through supported Datto SaaS Protection APIs.

**Kaseya and its affiliated entities make no representation or commitment regarding the ongoing maintenance, enhancement, development, availability or support of this application.**

By installing or using this application, users acknowledge and understand that:

1. **Independent Development:** The application is not part of Kaseya's officially supported software portfolio and should not be represented as a commercially supported Kaseya or Datto product.

2. **No Ongoing Maintenance Commitment:** Kaseya has no obligation to provide future updates, feature enhancements, compatibility fixes, security patches or continued development of the application.

3. **No Official Technical Support:** The application is not covered by Kaseya's standard product support arrangements, service-level agreements (SLAs), maintenance agreements or product lifecycle commitments.

4. **No Availability Guarantee:** Continued functionality may be affected by changes to Datto APIs, authentication mechanisms, third-party dependencies, operating systems or other external services.

5. **Client Responsibility:** Clients are responsible for evaluating the application's suitability for their environment, protecting their credentials and data, and maintaining appropriate operational safeguards.

6. **No Warranty:** The application is provided on an **"AS IS" and "AS AVAILABLE"** basis, without warranties or guarantees of functionality, accuracy, availability, security or fitness for a particular purpose.

This utility is intended to supplement existing Datto SaaS Protection functionality and does not replace officially supported vendor products, reporting mechanisms or technical support channels.

### Key Capabilities

- **Customer Portfolio Visibility:** Centralised view of customer domains, protected applications and seat coverage.
- **Backup Health Intelligence:** Clear distinction between healthy, imperfect and failed backup conditions.
- **Custom Risk Detection:** Additional identification of missing recent backups for active protected services.
- **Application Backup History:** Historical visibility into backup activity and protection trends.
- **Operational Alerts:** Identification and management of backup issues.
- **PDF Reporting:** Exportable application protection and backup history reports.
- **Audit Accountability:** Windows-authenticated access and user activity logging.
- **Scheduled Collection:** Automated data refresh every eight hours, with manual refresh capability.

---

## 2. Understanding Backup Health Classifications

The dashboard uses Datto API information together with supplementary assessment logic to classify backup health.

**Important:** Datto-reported failures and dashboard-detected risks are intentionally distinguished to avoid presenting an internally calculated condition as a confirmed Datto failure.

| Classification | Definition | Operational Interpretation |
|---|---|---|
| **Perfect** | No identified backup issues under the dashboard's evaluated criteria. | Protection appears healthy based on available information. |
| **Imperfect** | Between 1 and 9 active protected services have no recent backup, under the dashboard's custom evaluation. | Potential protection gaps exist and should be investigated. |
| **Failed — Datto** | Datto explicitly reports a failed backup condition through its API. | A vendor-reported backup failure requires attention. |
| **Failed — Custom Rule** | More than 9 active protected services have no recent backup, according to the dashboard's evaluation logic. | A significant protection gap has been detected internally, although Datto may not explicitly classify it as a failure. |
| **Unavailable** | Insufficient or unavailable backup information prevents a reliable assessment. | Collection or data availability should be reviewed before drawing conclusions. |

### Classification Principles

**Perfect — Healthy Backup Condition**

Indicates that the available backup information does not reveal a failure or an identified gap under the implemented assessment criteria. It is not a guarantee of successful restoration or complete data recoverability.

**Imperfect — Partial Protection Concern**

Indicates that 1–9 active protected services lack a recent backup according to the dashboard's assessment. These conditions warrant investigation but are not automatically vendor-confirmed failures.

**Failed — Datto-Reported Failure**

Represents a backup failure explicitly reported by Datto. This classification originates from the vendor's backup status information and should be investigated through the underlying Datto environment.

**Failed — Custom Rule**

Represents a dashboard-generated classification when more than nine active protected services are identified without a recent backup.

This additional logic helps surface potentially material protection gaps that may not otherwise be represented as explicit Datto failures.

**Unavailable — Insufficient Evidence**

Indicates that the available information cannot support a reliable backup-health determination. This may arise from missing data, incomplete collection or unavailable status information.

### Interpretation and Reporting Considerations

- Classification counts refer to **active protected services identified without a recent backup**, not necessarily the number of failed backup jobs.
- A custom-rule failure is **not equivalent to a Datto-confirmed failure**.
- The dashboard provides operational intelligence and does not replace vendor-side investigation, restore testing or independent recovery verification.
- Classifications reflect the information available at the time of the latest successful collection.

---

## 3. System Requirements

| Component | Requirement |
|---|---|
| Operating System | Compatible Microsoft Windows environment |
| Python Runtime | Python 3.14, as targeted by the V3 installer |
| Internet Connectivity | Required for initial downloads, dependency installation and Datto API access |
| Permissions | Ability to approve Windows UAC elevation when requested |
| Application Hosting | Local machine |
| Local Application Port | TCP 8501 |
| Virtual Environment | `%LOCALAPPDATA%\SaaSP\venv` |
| Browser | Modern browser capable of accessing localhost |

**Enterprise deployment note:** Corporate endpoint protection, proxy configuration, application control or Group Policy restrictions may affect installation. Any required approvals must follow the organisation's standard IT procedures.

---

## 4. Installation Procedure

### Step 1 — Extract the Release Package

Extract the complete release ZIP to a normal Windows directory.

Do not run installation scripts directly from within the compressed archive.

Maintain the supplied application directory structure and supporting files.

### Step 2 — Launch the Installer

Double-click:

`Install.bat`

The installer opens a Windows Command Prompt and displays installation progress.

Where elevated permissions are required, Windows User Account Control (UAC) will request administrator approval.

**Approve elevation only when authorised by your organisation. Do not execute the installer as the SYSTEM account.**

### Step 3 — Automated Installation

The installer performs the following operations:

1. Detects the required Python runtime.
2. Downloads the official Python installer when the required version is unavailable.
3. Creates the dedicated virtual environment at `%LOCALAPPDATA%\SaaSP\venv`.
4. Installs the application's required Python dependencies.
5. Checks the environment for dependency conflicts.
6. Reports installation success or failure.

The installer displays progress in the Command Prompt and retains the console on error to support troubleshooting.

### Step 4 — Installation Confirmation

When installation completes successfully, a Windows confirmation dialog is displayed.

**Expected confirmation:**

> Installation successful. All dependencies installed.
>
> Launch dashboard.bat to access the dashboard.

### Step 5 — Launch the Dashboard

Double-click:

`dashboard.bat`

The application starts locally and can be accessed at:

**http://127.0.0.1:8501**

Complete Windows authentication and the necessary Datto API configuration through the application interface.

---

## 5. Virtual Environment Architecture

### V3 Installation Improvement

Previous releases placed the Python virtual environment inside the extracted application directory.

On systems with unusually long extraction paths, this could cause Windows file-path limitations during dependency installation, including:

`pip ERROR 2 — No such file or directory`

**Version 3 addresses this issue** by placing the Python virtual environment at a dedicated, shorter path:

`%LOCALAPPDATA%\SaaSP\venv`

The application itself remains in the extracted release directory.

### Benefits

- Reduces exposure to long-path dependency installation errors.
- Separates application files from Python runtime dependencies.
- Keeps the client installation structure simpler.
- Avoids the need to distribute a development virtual environment.

---

## 6. Data Security and Credential Management

The client release is designed to avoid distributing development-specific or sensitive operational information.

### Excluded from the Release

- Development `.env` files and credentials
- Existing customer backup data and historical snapshots
- Development Python environments
- Cloud-specific application files and configurations
- Development-only resources not required for local operation

### Client Configuration

Datto API credentials must be supplied through the normal application configuration process after installation.

Existing credentials are retained if replacement credentials fail validation.

Successful credential replacement is subject to the application's confirmation workflow and audit controls.

**Security responsibility:** Client organisations remain responsible for protecting their API credentials, local application files and collected customer information according to their internal security requirements.

---

## 7. Dashboard Operations

### Automated Data Collection

The dashboard supports scheduled collection at eight-hour intervals, alongside manual refresh functionality.

Users should review the displayed collection status to determine whether information is current, incomplete or overdue.

### Monitoring and Investigation

Where collection problems or backup issues are identified, review the relevant dashboard alerts and diagnostic history.

For collection failures:

**Log → Refresh Run History → Inspect Refresh → Failed Customer Details**

### Backup Health Review

When investigating an issue:

1. Identify the affected customer and application.
2. Determine whether the classification is vendor-reported or dashboard-generated.
3. Review the latest available backup activity and underlying evidence.
4. Validate actual backup and recovery status through the appropriate Datto administration tools.
5. Record and manage operational alerts according to the organisation's procedures.

---

## 8. Troubleshooting

| Scenario | Recommended Action |
|---|---|
| Installer exits with an error | Review the error shown in the retained Command Prompt window. |
| Python installation fails | Check internet connectivity, Windows permissions, endpoint protection and corporate installation policies. |
| Dependency installation fails | Check the installer output, connectivity and dependency conflict information. |
| Dashboard does not launch | Verify successful installation and run `dashboard.bat` from the extracted application directory. |
| Browser cannot access the dashboard | Confirm the application is running and that local TCP port 8501 is available. |
| Datto API authentication fails | Verify the supplied API credentials and the client's authorised API access. |
| Backup information is missing or outdated | Review collection status and Refresh Run History before interpreting backup classifications. |

Do not disable endpoint security controls or bypass organisational restrictions to force an installation.

---

## 9. Release Validation and Testing

The following release preparation checks have been completed:

| Validation Area | Status |
|---|---|
| ZIP archive integrity | Completed |
| Python syntax verification | Completed |
| Packaging structure checks | Completed |
| Clean Windows installation | Pending client-side validation |
| End-to-end dashboard launch | Pending client-side validation |
| Client authentication and API access | Pending client-side validation |

**Release qualification:** Successful packaging checks do not constitute end-to-end deployment certification.

The installer must be executed on a representative clean Windows machine to confirm runtime compatibility, dependency installation, authentication and dashboard operation.

Corporate policies may restrict downloads, UAC elevation or installation even when the release package itself passes validation.

---

## 10. Operational Disclaimer

SaaS Protection Intelligence provides an enhanced operational reporting and assessment layer using available Datto SaaS Protection information.

Dashboard-generated classifications, including **Failed — Custom Rule**, represent supplementary analytical findings and should not be interpreted as official vendor-reported backup failures.

The dashboard does not independently certify data recoverability, guarantee successful restoration or replace the source platform's operational controls.

**All critical backup and recovery decisions should be verified against the underlying Datto environment and the organisation's established recovery procedures.**

---

**SaaS Protection Intelligence — Windows Client Release V3**  
*Enhanced Visibility. Clearer Risk Classification. Operational Accountability.*