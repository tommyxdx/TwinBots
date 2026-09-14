from __future__ import annotations
import argparse
import asyncio
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import shutil
import time
from .config import load_config
from .storage import Store
from .net import HTTP
from .data import Fetcher
from .notify import Telegram
from .scanner import Scanner
from .models import train_cex,train_scanner
from .runtime import ProcessLock,maintain,maintenance_loop,scanner_loop
from .cex import CexPaper
from .dex import DexPaper,QuoteGateway
from .report import export_report
from .demo import run_demo


def output(data):
    print(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False))


def parser():
    p = argparse.ArgumentParser(description="TwinCryptoBots — scanner + paper trading, no real orders")
    p.add_argument("--config",default="config.yaml")
    p.add_argument("--verbose",action="store_true")
    commands = p.add_subparsers(dest="command",required=True)
    commands.add_parser("init",help="Create local config.yaml and .env without overwriting")
    d = commands.add_parser("doctor",help="Validate environment and optional public connectivity")
    d.add_argument("--online",action="store_true")
    f = commands.add_parser("fetch",help="Independent public history downloader")
    f.add_argument("--watch",action="store_true")
    f.add_argument("--pool-pages",type=int,default=1)
    t = commands.add_parser("train")
    t.add_argument("--target",choices=("cex","scanner","all"),default="all")
    s = commands.add_parser("scan")
    s.add_argument("--once",action="store_true")
    t = commands.add_parser("trade")
    t.add_argument("--venue",choices=("cex","dex","both"),default="cex")
    commands.add_parser("run",help="Scanner + enabled paper venues + hourly maintenance")
    commands.add_parser("report")
    demo = commands.add_parser("demo",help="Offline synthetic behavior demo, never performance evidence")
    demo.add_argument("--output",default="demo_output")
    return p


def init(path):
    root = Path(path).resolve().parent
    root.mkdir(parents=True,exist_ok=True)
    source = Path(__file__).resolve().parent.parent
    for template,target in (("config.example.yaml",Path(path).name),(".env.example",".env")):
        dest = root/target
        if not dest.exists():
            shutil.copyfile(source/template,dest)
    return {"config":str(Path(path).resolve()),"env":str(root/".env"),"mode":"paper"}


def doctor(cfg,store,http,online):
    result = {"mode":"paper_only","data_dir":cfg["data_dir"],
              "binance_key_present":bool(os.getenv("BINANCE_API_KEY")),
              "public_binance_key_required":False,
              "telegram_enabled":cfg["telegram"]["enabled"],
              "telegram_ready":all(os.getenv(cfg["telegram"][k]) for k in ("token_env","chat_id_env")),
              "dex_enabled":cfg["dex"]["enabled"],"dex_provider":cfg["dex"]["provider"],
              "security_adapter_configured":bool(cfg["scanner"]["feature_url_template"]),
              "remote_scanner_dataset_configured":bool(cfg["history"]["scanner_dataset_url"]),
              "note":"Absent optional credentials do not block CEX public-data paper trading; no 100x pretrained model is bundled."}
    if online:
        checks = {}
        for name,url in (("binance",cfg["cex"]["rest_base"]+"/api/v3/time"),
                         ("geckoterminal",cfg["scanner"]["gecko_base"]+"/networks/"+cfg["scanner"]["network"]+"/new_pools?page=1")):
            try:
                data = http.json(url)
                checks[name] = {"reachable":True}
                if name=="binance":
                    checks[name]["clock_difference_ms"] = round(time.time()*1000-data["serverTime"])
            except Exception as exc:
                checks[name] = {"reachable":False,"error":str(exc)}
        result["online"] = checks
    return result


async def services(args,cfg,store,http,fetcher):
    scanner = Scanner(cfg,store,http,fetcher,Telegram(cfg,store,http))
    with ExitStack() as stack:
        use_scan = args.command in ("run","scan")
        venues = []
        if args.command=="run":
            venues = [v for v in ("cex","dex") if cfg[v]["enabled"]]
        elif args.command=="trade":
            venues = ["cex","dex"] if args.venue=="both" else [args.venue]
            if "dex" in venues and not cfg["dex"]["enabled"]:
                raise ValueError("Set dex.enabled: true after filling quote/security interfaces")
        for name in (["scanner"] if use_scan else [])+venues:
            stack.enter_context(ProcessLock(store.root,name))
        if cfg["bootstrap_on_start"]:
            try:
                await asyncio.to_thread(maintain,cfg,store,fetcher,True)
            except Exception as exc:
                logging.warning("Bootstrap incomplete: %s; available services will still start",exc)
        if args.command=="scan" and args.once:
            output(await asyncio.to_thread(scanner.run_once))
            return
        tasks = [asyncio.create_task(maintenance_loop(cfg,store,fetcher))]
        if use_scan:
            tasks.append(asyncio.create_task(scanner_loop(scanner,cfg)))
        if "cex" in venues:
            tasks.append(asyncio.create_task(CexPaper(cfg,store,http,fetcher).run()))
        if "dex" in venues:
            tasks.append(asyncio.create_task(DexPaper(cfg,store,QuoteGateway(cfg,http)).run()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # Avoid verbose websocket internals/headers in logs.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if args.command=="init":
        output(init(args.config))
        return 0
    path = args.config
    if args.command=="demo" and not Path(path).exists():
        path = str(Path(__file__).resolve().parent.parent/"config.example.yaml")
    cfg = load_config(path)
    if args.command=="demo":
        output(run_demo(cfg,args.output))
        return 0
    store = Store(cfg["data_dir"])
    http = HTTP(cfg,store)
    fetcher = Fetcher(cfg,store,http)
    try:
        if args.command=="doctor":
            output(doctor(cfg,store,http,args.online))
        elif args.command=="report":
            output({"report":export_report(cfg,store)})
        elif args.command=="train":
            with ProcessLock(store.root,"maintenance"):
                output({k:(train_cex if k=="cex" else train_scanner)(cfg,store)
                        for k in (("cex","scanner") if args.target=="all" else (args.target,))})
        elif args.command=="fetch":
            if not 1<=args.pool_pages<=20:
                raise ValueError("--pool-pages must be between 1 and 20")
            while True:
                with ProcessLock(store.root,"maintenance"):
                    result = {"bootstrap":fetcher.bootstrap()}
                    try:
                        result["discovered"] = len(fetcher.discover())
                        result["cohort"] = fetcher.follow_cohort(args.pool_pages)
                    except Exception as exc:
                        result["scanner_error"] = str(exc)
                    output(result)
                if not args.watch:
                    break
                # Interruptible idle; hourly archive/cohort job is cheap.
                for _ in range(3600):
                    time.sleep(1)
        else:
            asyncio.run(services(args,cfg,store,http,fetcher))
    except KeyboardInterrupt:
        logging.info("Stopped. Paper state retained; restart will reconcile local pending orders.")
    finally:
        store.close()
    return 0
