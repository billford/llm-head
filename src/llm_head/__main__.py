"""Command line: `llm-head serve -c config.yaml` and `llm-head check-config -c config.yaml`."""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llm-head", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("serve", "run the load balancer"), ("check-config", "validate a config file and exit")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("-c", "--config", required=True)
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "check-config":
        hosts = ", ".join(f"{h.name} ({h.vram_mb} MB)" for h in cfg.hosts)
        print(f"ok: {len(cfg.hosts)} hosts [{hosts}], listening on {cfg.server.host}:{cfg.server.port}")
        return 0

    import uvicorn

    from .app import build_app

    app = build_app(cfg)
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=int(cfg.server.shutdown_timeout),
        h11_max_incomplete_event_size=cfg.server.request_limits.max_header_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
