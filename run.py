"""MGDM scoring - command line entry point.

  python run.py serve      start the web app (phones + laptop)
  python run.py sheets     generate printable scoresheet PDFs into output/sheets (one per event)
  python run.py sheets school   ...one packet per school instead (output/sheets/by_school)
  python run.py export     rebuild the Excel master (and Google mirror) now
  python run.py practice   make filled-in practice "phone photos" in output/practice
  python run.py selftest   read the practice photos and report accuracy
"""
import logging
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent


def load_env():
    env = ROOT / "config" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "sheets":
        from mgdm.config import load_config, load_entries
        from mgdm.sheets_pdf import generate
        cfg = load_config()
        by = "school" if "school" in sys.argv[2:] else "event"
        for p in generate(cfg, load_entries(cfg), ROOT / "output" / "sheets", by=by):
            print("wrote", p)
    elif cmd == "export":
        from mgdm.service import STATE, publish
        publish(background=False)
        print(STATE.sync_status)
    elif cmd in ("practice", "selftest"):
        from mgdm.config import load_config, load_entries
        from mgdm import selftest
        cfg = load_config()
        if cmd == "practice":
            print(len(selftest.make_practice(cfg, load_entries(cfg))), "photos in", selftest.OUT)
        else:
            selftest.selftest(cfg, load_entries(cfg), int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    elif cmd == "serve":
        from mgdm.app import app, lan_url, start_inbox_watcher
        from mgdm.service import STATE, publish
        publish()
        start_inbox_watcher()
        s = STATE.cfg.settings.get("app", {})
        print(f"\n  MGDM scoring running.  On this laptop: http://localhost:{s.get('port', 5050)}"
              f"\n  Phones on the same Wi-Fi: {lan_url()}\n")
        app.run(host=s.get("host", "0.0.0.0"), port=int(s.get("port", 5050)), threaded=True)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
