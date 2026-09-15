from __future__ import annotations
import argparse
import asyncio
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import time
from .config import load_config
from .storage import Store
from .net import HTTP
from .data import Fetcher
from .notify import Telegram
from .scanner import Scanner
from .wallet_scanner import WalletScanner
from .models import train_cex,train_scanner
from .runtime import ProcessLock,maintain,maintenance_loop,scanner_loop
from .cex import CexPaper
from .dex import DexPaper,QuoteGateway
from .follow import ActivityFeed,CopyTrader
from .report import export_report
from .demo import run_demo


def output(data):
    print(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False))


def candidates(cfg,store):
    """Addresses worth a ledger, best-justified first.

    Your own list leads, then the providers' shortlist, then on-chain discovery,
    because reconstruction is the scarce resource and the order decides what gets
    spent on first.
    """
    short = store.get("wallet:shortlist",{}).get("addresses",[])
    found = store.get(f"wallet:discovery:{cfg['scanner']['network']}",{}).get("addresses",{})
    discovered = sorted(found,key=lambda a:(-found[a],a))
    ordered = cfg["wallets"]["addresses"]+short+discovered
    return list(dict.fromkeys(ordered))[:cfg["wallets"]["max_candidates"]]


KNOWN_PROVIDER_KEYS = ("DUNE_API_KEY","BIRDEYE_API_KEY","SOLSCAN_API_KEY")


def refresh_shortlist(cfg,store,http):
    """Pull candidate addresses from the configured providers and merge them."""
    from adapters.shortlist import collect,rank_by_agreement
    sources = cfg["shortlist"]["sources"]
    if not sources:
        # A key in .env does nothing on its own: something has to say which
        # endpoint to call and where the addresses sit in its response.
        hint = "No sources under shortlist: in config.yaml"
        present = [k for k in KNOWN_PROVIDER_KEYS if os.getenv(k)]
        if present:
            hint += f" ({', '.join(present)} is set, but no source uses it)"
        return {"addresses":0,"sources":[],"hint":hint}
    merged,report = collect(sources,http)
    addresses = rank_by_agreement(merged,cfg["shortlist"]["max_addresses"])
    store.set("wallet:shortlist",{"at":time.time(),"addresses":addresses,
                                  "sources":{a:merged[a]["sources"] for a in addresses},
                                  "report":report,
                                  "note":"A coarse screen from third parties, never a ranking"})
    return {"addresses":len(addresses),"sources":report}


async def shortlist_loop(cfg,store,http):
    while True:
        try:
            result = await asyncio.to_thread(refresh_shortlist,cfg,store,http)
            logging.info("Shortlist: %d candidate(s) from %d source(s)",
                         result["addresses"],len(result["sources"]))
        except Exception as exc:
            logging.warning("Shortlist refresh failed: %s",exc)
            store.event("shortlist_error",{"type":type(exc).__name__})
        await asyncio.sleep(cfg["shortlist"]["refresh_s"])


def refresh_ledgers(cfg,store):
    """Rebuild a few ledgers from chain per cycle, newest attempt last.

    Failures are recorded with their reason so the report shows the real
    rejection mix without anyone running a separate diagnostic pass.
    """
    from adapters.helius import Helius
    from adapters.ledger import build
    from adapters.prices import SolPrice
    w = cfg["wallets"]
    folder = Path(w["ledger_dir"])
    folder.mkdir(parents=True,exist_ok=True)
    state = store.get("wallet:build",{})
    now = time.time()
    def stale(address):
        # A build that errored may have failed on something transient, so it is
        # retried on a shorter clock than a ledger that actually reconstructed.
        last = state.get(address,{})
        settled = "usable" in last and "error" not in last
        wait = w["ledger_build_refresh_s"] if settled else w["ledger_retry_s"]
        return now-last.get("at",0) >= wait
    due = [a for a in candidates(cfg,store) if stale(a)]
    if not due:
        return {"built":0,"pending":0}
    due.sort(key=lambda a: state.get(a,{}).get("at",0))
    helius,prices = Helius(),SolPrice(Path(cfg["data_dir"])/"downloads")
    built,screened,spent_at_start = 0,0,0
    blocked = {}
    for address in due[:w["ledgers_per_cycle"]]:
        # Rejections are cheap and full backfills are not, so the cycle is capped
        # by calls spent rather than by wallets looked at.
        if helius.calls-spent_at_start >= w["ledger_calls_per_cycle"]:
            break
        screened += 1
        path = folder/(address+".json")
        try:
            ledger,report = build(address,helius,prices,min_history_days=w["min_history_days"],
                                  max_pages=w["ledger_max_pages"])
        except Exception as exc:
            # Keep the message, not just the class: "HTTPError" alone cannot tell
            # a missing price archive from a rejected key.
            state[address] = {"at":now,"usable":False,
                              "error":f"{type(exc).__name__}: {exc}"[:200]}
            store.set("wallet:build",state)
            logging.warning("Ledger build failed for %s: %s",address[:8],exc)
            continue
        if ledger is None:
            # A wallet that stopped qualifying must not keep ranking on stale data.
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(ledger,ensure_ascii=False),encoding="utf-8")
            built += 1
        if not report.get("usable"):
            reason = report.get("blocked_by","unknown")
            blocked[reason] = blocked.get(reason,0)+1
        state[address] = {"at":now,**{k:v for k,v in report.items() if k!="address"}}
        store.set("wallet:build",state)
    store.set("heartbeat:ledgers",time.time())
    return {"screened":screened,"built":built,"blocked":blocked,
            "rpc_calls":helius.calls-spent_at_start,"pending":max(0,len(due)-screened)}


async def ledger_loop(cfg,store):
    # Discovery runs in the scanner, so there is nothing to build on the first tick.
    await asyncio.sleep(20)
    while True:
        try:
            result = await asyncio.to_thread(refresh_ledgers,cfg,store)
            if result["screened"]:
                # Progress has to be visible, or a slow queue reads as a hang.
                reasons = ", ".join(f"{k} {v}" for k,v in sorted(result["blocked"].items()))
                logging.info("Ledgers: screened %d, built %d, %d left, %d RPC calls%s",
                             result["screened"],result["built"],result["pending"],
                             result["rpc_calls"],f" ({reasons})" if reasons else "")
        except Exception as exc:
            logging.warning("Ledger refresh unavailable: %s",exc)
            store.event("ledger_build_error",{"type":type(exc).__name__})
        await asyncio.sleep(cfg["wallets"]["ledger_build_every_s"])


async def activity_loop(cfg,store):
    """Keep the followed leaders' fill feeds fresh; the copy trader reads the files."""
    from adapters.activity import poll_once
    from adapters.helius import Helius
    from adapters.prices import SolPrice
    f,client = cfg["follow"],None
    while True:
        try:
            leaders = (store.get("copy:leaders") or {}).get("addresses",[])
            if leaders:
                # Built on first use so a missing key idles this loop instead of
                # taking down the venues running alongside it.
                if client is None:
                    client = (Helius(),SolPrice(Path(cfg["data_dir"])/"downloads"))
                await asyncio.to_thread(poll_once,leaders,client[0],client[1],
                                        f["activity_dir"],f["activity_lookback_s"])
                store.set("heartbeat:activity",time.time())
        except Exception as exc:
            logging.warning("Leader activity unavailable: %s",exc)
            store.event("activity_poll_error",{"type":type(exc).__name__})
        await asyncio.sleep(f["activity_poll_s"])


async def report_loop(cfg,store,every_s=3600):
    while True:
        await asyncio.sleep(every_s)
        try:
            await asyncio.to_thread(export_report,cfg,store)
        except Exception as exc:
            logging.warning("Report export failed: %s",exc)


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
    t.add_argument("--venue",choices=("cex","dex","copy","both"),default="cex")
    t.add_argument("--close-positions",action="store_true",
                   help="Sell everything before exiting instead of keeping it open")
    r = commands.add_parser("run",help="Scanner + enabled paper venues + hourly maintenance")
    r.add_argument("--close-positions",action="store_true",
                   help="Sell everything before exiting instead of keeping it open")
    sl = commands.add_parser("shortlist",help="Query the candidate providers once and print the merge")
    sl.add_argument("--probe",metavar="URL",
                    help="Fetch one URL and report which paths hold Solana addresses")
    sl.add_argument("--header",action="append",default=[],metavar="NAME:VALUE",
                    help="Header for --probe; use NAME:env:VAR to read a key from .env")
    sl.add_argument("--param",action="append",default=[],metavar="NAME=VALUE",
                    help="Query parameter for --probe")
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
              "scanner_kind":cfg["scanner"]["kind"],
              "wallet_source":cfg["wallets"]["source"],
              "wallet_ledger_dir":cfg["wallets"]["ledger_dir"],
              "wallet_adapter_configured":bool(cfg["wallets"]["url_template"]),
              "copy_trading_enabled":cfg["follow"]["enabled"],
              "copy_activity_source":cfg["follow"]["source"],
              "copy_activity_dir":cfg["follow"]["activity_dir"],
              "copy_adapter_configured":bool(cfg["follow"]["url_template"]),
              "note":"Wallet discovery does not provide complete cost history. Supply normalized ledgers for ranking; no trading keys needed."}
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


async def supervise(tasks,engines,cfg,store,close_positions,grace_s=10):
    """Run until a task ends or the operator interrupts, then wind down in order.

    The interrupt is turned into an event inside the loop rather than letting
    KeyboardInterrupt unwind it from outside: the venue feeds hold open TLS
    sockets, and closing those needs the loop it is about to tear down.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    previous = None
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT,lambda *_: loop.call_soon_threadsafe(stop.set))
    except (ValueError,OSError):
        pass  # Not the main thread; KeyboardInterrupt handling stays with the caller.
    watcher = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait([*tasks,watcher],return_when=asyncio.FIRST_COMPLETED)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT,previous)
        watcher.cancel()
        logging.info("Stopping: %s, then final report. Paper state is retained.",
                     "closing positions" if close_positions else "keeping open positions")
        # Sell before the feeds stop: an exit needs a live book or a live quote.
        if close_positions:
            for engine in engines:
                try:
                    done = await asyncio.wait_for(engine.liquidate(),timeout=grace_s)
                    logging.info("%s liquidated %d position(s) on exit",
                                 getattr(engine,"venue","cex").upper(),len(done))
                except Exception as exc:
                    logging.warning("Liquidation incomplete: %s",exc)
        for task in tasks:
            task.cancel()
        # A bounded window so a hung socket cannot block the shutdown forever.
        await asyncio.wait([*tasks,watcher],timeout=grace_s)
        try:
            await asyncio.to_thread(export_report,cfg,store)
        except Exception as exc:
            logging.warning("Final report not written: %s",exc)


async def services(args,cfg,store,http,fetcher):
    wallet_mode = cfg["scanner"]["kind"] == "wallets"
    scanner = (WalletScanner if wallet_mode else Scanner)(cfg,store,http,fetcher,Telegram(cfg,store,http))
    with ExitStack() as stack:
        use_scan = args.command in ("run","scan")
        venues = []
        if args.command=="run":
            enabled = {"cex":cfg["cex"]["enabled"],"dex":cfg["dex"]["enabled"],"copy":cfg["follow"]["enabled"]}
            venues = [v for v in ("cex","dex","copy") if enabled[v]]
        elif args.command=="trade":
            venues = ["cex","dex"] if args.venue=="both" else [args.venue]
            if "dex" in venues and not cfg["dex"]["enabled"]:
                raise ValueError("Set dex.enabled: true after filling quote/security interfaces")
            if "copy" in venues and not cfg["follow"]["enabled"]:
                raise ValueError("Set follow.enabled: true and configure a leader activity feed")
        for name in (["scanner"] if use_scan else [])+venues:
            stack.enter_context(ProcessLock(store.root,name))
        wallet_scan_only = wallet_mode and args.command == "scan"
        if cfg["bootstrap_on_start"] and not wallet_scan_only:
            try:
                await asyncio.to_thread(maintain,cfg,store,fetcher,True)
            except Exception as exc:
                logging.warning("Bootstrap incomplete: %s; available services will still start",exc)
        if args.command=="scan" and args.once:
            output(await asyncio.to_thread(scanner.run_once))
            return
        tasks = [] if wallet_scan_only else [asyncio.create_task(maintenance_loop(cfg,store,fetcher))]
        if use_scan:
            tasks.append(asyncio.create_task(scanner_loop(scanner,cfg)))
            if wallet_mode and cfg["wallets"]["source"]=="chain":
                tasks.append(asyncio.create_task(ledger_loop(cfg,store)))
            if wallet_mode and cfg["shortlist"]["enabled"]:
                tasks.append(asyncio.create_task(shortlist_loop(cfg,store,http)))
        if "copy" in venues and cfg["follow"]["source"]=="local" and cfg["wallets"]["source"]=="chain":
            tasks.append(asyncio.create_task(activity_loop(cfg,store)))
        if args.command=="run":
            tasks.append(asyncio.create_task(report_loop(cfg,store)))
        engines = []
        if "cex" in venues:
            engines.append(CexPaper(cfg,store,http,fetcher))
        if "dex" in venues:
            engines.append(DexPaper(cfg,store,QuoteGateway(cfg,http)))
        if "copy" in venues:
            engines.append(CopyTrader(cfg,store,QuoteGateway(cfg,http),ActivityFeed(cfg,http)))
        tasks += [asyncio.create_task(engine.run()) for engine in engines]
        await supervise(tasks,engines,cfg,store,getattr(args,"close_positions",False))


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
        elif args.command=="shortlist":
            if args.probe:
                from adapters.shortlist import probe
                headers = dict(h.split(":",1) for h in args.header)
                params = dict(p.split("=",1) for p in args.param)
                wallets,others,keys = probe(args.probe,headers,http,params or None)
                output({"top_level_keys":keys,"wallet_path_candidates":wallets,
                        "not_wallets":others,
                        "next":"Use a wallet_path_candidates entry; confirm the field means "
                               "a trader, not a token or pool"})
            else:
                output(refresh_shortlist(cfg,store,http))
        elif args.command=="report":
            output({"report":export_report(cfg,store)})
        elif args.command=="train":
            if cfg["scanner"]["kind"] == "wallets" and args.target == "scanner":
                output({"scanner":"Wallet ranking is deterministic and needs no model training"})
                return 0
            targets = ("cex",) if cfg["scanner"]["kind"] == "wallets" else ("cex","scanner")
            with ProcessLock(store.root,"maintenance"):
                output({k:(train_cex if k=="cex" else train_scanner)(cfg,store)
                        for k in (targets if args.target=="all" else (args.target,))})
        elif args.command=="fetch":
            if not 1<=args.pool_pages<=20:
                raise ValueError("--pool-pages must be between 1 and 20")
            while True:
                with ProcessLock(store.root,"maintenance"):
                    result = {"bootstrap":fetcher.bootstrap()}
                    if cfg["scanner"]["kind"] == "tokens":
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
