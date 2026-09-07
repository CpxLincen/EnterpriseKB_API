"""兼容入口：`python -m app.eval` 仍指向 services.eval.main。"""

from app.services.eval import main

if __name__ == "__main__":
    raise SystemExit(main())
