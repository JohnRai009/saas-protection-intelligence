import sys
import subprocess
from pathlib import Path

TASK_NAME = "Datto SaaS Protection Auto Refresh"


def run_scheduled_check():
    from collection_scheduler import scheduled_check

    scheduled_check()


def install_scheduler():
    script = Path(__file__).resolve()
    python_exe = Path(sys.executable)

    # Run without displaying a Windows console.
    pythonw_exe = python_exe.with_name("pythonw.exe")

    if not pythonw_exe.exists():
        pythonw_exe = python_exe

    command = f'"{pythonw_exe}" "{script}" --run'

    subprocess.run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/SC", "MINUTE",
            "/MO", "5",
            "/TN", TASK_NAME,
            "/TR", command,
        ],
        check=True,
    )

    print("SUCCESS: Automatic refresh scheduler installed.")
    print("Windows checks every 5 minutes.")
    print("Datto collection runs only when the 8-hour refresh is due.")
    print("Existing dashboard settings and API safeguards are respected.")


if __name__ == "__main__":
    if "--run" in sys.argv:
        run_scheduled_check()
    else:
        install_scheduler()