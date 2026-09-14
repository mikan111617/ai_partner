from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .memory import MemoryStore
from .runtime import PartnerRuntime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local AI Partner")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--mode", choices=["auto", "normal", "game"])
    parser.add_argument("--reset-conversation", action="store_true", help="delete conversation turns but keep psychology/game events")
    parser.add_argument("--no-mic", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    if args.no_mic:
        cfg["microphone"]["enabled"] = False

    data_dir = Path(args.data_dir)
    if args.reset_conversation:
        store = MemoryStore(data_dir / "memory.sqlite3")
        count = store.reset_conversation()
        store.close()
        print(f"[memory] deleted conversation turns={count}; psychology and game events kept")
        return 0

    print("[character] エリシア / お嬢様 (fixed public character)")
    print("[identity] no user name or fixed user address is configured")
    print(f"[models] normal={cfg['ollama']['conversation_model']} | game={cfg['ollama']['game_model']}")
    PartnerRuntime(cfg, data_dir).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
