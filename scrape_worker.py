import datetime
import json
import sys

import scraper


EVENT_PREFIX = "__SCRAPER_EVENT__"


def emit_event(payload):
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def main(date_args):
    if not date_args:
        print("未提供采集日期", file=sys.stderr, flush=True)
        return 2

    failed = False
    try:
        for index, value in enumerate(date_args, start=1):
            day = datetime.datetime.strptime(value, "%Y-%m-%d").date()
            chinese_date = day.strftime("%Y年%m月%d日")
            emit_event({
                "type": "date_start", "date": chinese_date,
                "index": index, "total": len(date_args),
            })
            result = scraper.run_scraper_for_date(chinese_date)
            emit_event({"type": "date_result", "date": chinese_date, "result": result})
            if result.get("status") == "failed":
                failed = True
    finally:
        scraper.release_model()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
