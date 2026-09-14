"""Real local HTTP, archive verification, scanner and CEX arrival-time tests.

All market values here are SYNTHETIC; these tests are not strategy backtests.
"""
import asyncio
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from twobots.config import load_config
from twobots.storage import Store
from twobots.net import HTTP
from twobots.data import Fetcher
from twobots.scanner import Scanner
from twobots.notify import Telegram
from twobots.cex import CexPaper
from twobots.execution import Book
from twobots.dex import QuoteGateway

ROOT=Path(__file__).resolve().parents[1]


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=load_config(ROOT/"config.example.yaml")
        self.store=Store(self.tmp.name)
        self.cfg["data_dir"]=self.tmp.name
        self.cfg["http"].update(retries=0,min_interval_s=0,host_intervals={})
        self.cfg["cex"].update(latency_ms=[10,10],lost_ack_probability=0,adverse_fill_bps=0,
                               visible_liquidity_fraction=1,slippage_limit_bps=500)
        self.cfg["history"]["symbols"]=["BTCUSDT"]
        self.cfg["cex"]["symbols"]=["BTCUSDT"]
        self.requests=[]
        self.zipbuf=io.BytesIO()
        with zipfile.ZipFile(self.zipbuf,"w") as z:
            z.writestr("BTCUSDT-4h-2025-06.csv","1750000000000000,10,11,9,10.5,100,1750014399999999,1050,10,50,500,0\n")
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def do_GET(self):
                path=urlparse(self.path)
                owner.requests.append(path.path)
                qs=parse_qs(path.query)
                now=int(time.time()//300)*300
                if path.path.endswith(".CHECKSUM"):
                    body=hashlib.sha256(owner.zipbuf.getvalue()).hexdigest().encode()+b"  archive.zip"
                elif path.path.endswith(".zip"):
                    body=owner.zipbuf.getvalue()
                else:
                    if path.path.endswith("/new_pools"):
                        data={"data":[{"attributes":{"address":"POOL","pool_created_at":now-86400,
                             "reserve_in_usd":"100000","volume_usd":{"h1":"60000"},
                             "transactions":{"h1":{"buys":50,"sells":30}}},
                             "relationships":{"base_token":{"data":{"id":"solana_TOKEN"}}}}],
                             "included":[{"id":"solana_TOKEN","attributes":{"symbol":"SYNTHETIC"}}]}
                    elif "/ohlcv/" in path.path:
                        data={"data":{"attributes":{"ohlcv_list":[[now-(240-i)*300,10+i*.01,10.15+i*.01,9.99+i*.01,10.1+i*.01,1000+i*10]
                              for i in range(240)]}}}
                    elif path.path=="/features":
                        data={"network":"solana","token":"TOKEN","pool":"POOL","asof":time.time(),
                              "source":"SYNTHETIC_TEST","features":{"can_sell":True,"mint_revoked":True,"freeze_revoked":True}}
                    elif path.path=="/quote":
                        data={"input_token":qs["input_token"][0],"output_token":qs["output_token"][0],
                              "in_amount":qs["amount"][0],"out_amount":"1000","min_out":"990",
                              "price_impact_fraction":.001,"asof":time.time()}
                    elif path.path.endswith("/exchangeInfo"):
                        data={"symbols":[{"symbol":"BTCUSDT","status":"TRADING","quoteAsset":"USDT","filters":[
                            {"filterType":"LOT_SIZE","stepSize":"0.01","minQty":"0.01","maxQty":"10000"},
                            {"filterType":"PRICE_FILTER","tickSize":"0.01","minPrice":"0.01","maxPrice":"1000000"},
                            {"filterType":"MIN_NOTIONAL","minNotional":"5"}]}]}
                    else:
                        self.send_error(404); return
                    body=json.dumps(data).encode()
                self.send_response(200); self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base=f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cfg["history"]["archive_base"]=self.base
        self.cfg["scanner"]["gecko_base"]=self.base
        self.cfg["scanner"]["feature_url_template"]=self.base+"/features?network={network}&token={token}&pool={pool}"
        self.cfg["cex"]["rest_base"]=self.base
        self.http=HTTP(self.cfg,self.store)
        self.fetcher=Fetcher(self.cfg,self.store,self.http)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.store.close(); self.tmp.cleanup()

    def engine(self):
        c=CexPaper(self.cfg,self.store,self.http,self.fetcher)
        c.metadata()
        b=Book(); b.snapshot({"lastUpdateId":1,"bids":[[9.9,5]],"asks":[[10,.2],[10.1,.3],[12,10]]})
        c.books["BTCUSDT"]=b
        return c

    def test_archive_checksum_cache_and_import(self):
        self.assertEqual(self.fetcher.binance_archive("BTCUSDT","2025-06"),1)
        count=len(self.requests)
        self.assertEqual(self.fetcher.binance_archive("BTCUSDT","2025-06"),1)
        self.assertEqual(len(self.requests),count)
        self.assertEqual(self.store.candles("cex","BTCUSDT")[0]["ts"],1750000000)

    def test_tampered_archive_detected(self):
        self.fetcher.binance_archive("BTCUSDT","2025-06")
        (self.store.root/"downloads"/"BTCUSDT-4h-2025-06.zip").write_bytes(b"bad")
        with self.assertRaises(ValueError): self.fetcher.binance_archive("BTCUSDT","2025-06")

    def test_scanner_http_to_persisted_result(self):
        s=Scanner(self.cfg,self.store,self.http,self.fetcher,Telegram(self.cfg,self.store,self.http))
        result=s.run_once()
        self.assertEqual(len(result),1)
        self.assertTrue(result[0]["security_verified"])
        self.assertTrue(result[0]["history_fresh"])
        self.assertIsNone(result[0]["probabilities"]["x100_30d"]["probability"])
        self.assertEqual(len(self.store.rows("SELECT * FROM scans")),1)

    def test_generic_quote_over_http(self):
        self.cfg["dex"].update(provider="generic",generic_quote_url=self.base+"/quote")
        q=QuoteGateway(self.cfg,self.http).quote("A","B",2000000)
        self.assertEqual(q["in_amount"],2000000)
        self.assertEqual(q["out_amount"],1000)

    def test_cex_real_engine_partial_fill(self):
        c=self.engine()
        r=asyncio.run(c.order("BTCUSDT","BUY",1,"test"))
        self.assertEqual(r["settled_status"],"PARTIALLY_FILLED_CANCELED")
        self.assertAlmostEqual(r["filled_qty"],.5)
        self.assertAlmostEqual(c.ledger.state()["cash"],100-(.2*10+.3*10.1)*1.001)

    def test_latency_changes_arrival_price_no_fill(self):
        c=self.engine()
        async def scenario():
            def move():
                c.books["BTCUSDT"].snapshot({"lastUpdateId":2,"bids":[[11,1]],"asks":[[12,1]]})
            asyncio.get_running_loop().call_later(.002,move)
            return await c.order("BTCUSDT","BUY",1,"test")
        r=asyncio.run(scenario())
        self.assertEqual(r["settled_status"],"EXPIRED")
        self.assertEqual(c.ledger.state()["cash"],100)

    def test_stale_at_arrival_cannot_fill(self):
        c=self.engine()
        async def scenario():
            asyncio.get_running_loop().call_later(.002,lambda:setattr(c.books["BTCUSDT"],"received",time.time()-10))
            return await c.order("BTCUSDT","BUY",1,"test")
        r=asyncio.run(scenario())
        self.assertEqual(r["settled_status"],"EXPIRED_STALE_BOOK")
        self.assertEqual(c.ledger.state()["cash"],100)

    def test_min_notional_rejects_small_order(self):
        c=self.engine()
        r=asyncio.run(c.order("BTCUSDT","BUY",.1,"test"))
        self.assertEqual(r["status"],"FILTER_REJECTED")
        self.assertFalse(self.store.rows("SELECT * FROM orders"))


if __name__=="__main__": unittest.main()
