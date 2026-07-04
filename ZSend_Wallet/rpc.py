from __future__ import annotations

import binascii
import json

import requests
from requests.auth import HTTPBasicAuth

class RPCError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


class BitcoinZRPC:

    def __init__(self, host="127.0.0.1", port=1979, user="", password=""):
        self.host     = host
        self.port     = port
        self.user     = user
        self.password = password
        self.url      = f"http://{host}:{port}/"
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(user, password)
        self._session.headers.update({"Content-Type": "text/plain"})
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8, pool_maxsize=8, max_retries=0
        )
        self._session.mount("http://", adapter)
        self._lock = __import__("threading").Lock()

    def endpoint(self) -> tuple[str, int, str, str]:
        return (self.host, self.port, self.user, self.password)

    def clone(self) -> "BitcoinZRPC":
        return BitcoinZRPC(*self.endpoint())

    def call(self, method: str, params: list, timeout: int = 30):
        body = json.dumps({
            "jsonrpc": "1.0",
            "id":      "curltest",
            "method":  method,
            "params":  params,
        })
        try:
            with self._lock:
                r = self._session.post(self.url, data=body, timeout=timeout)
        except requests.exceptions.ConnectionError:
            raise RPCError(f"Connection refused -> {self.url}")
        except requests.exceptions.Timeout:
            raise RPCError(f"[{method}] timed out after {timeout}s")
        except Exception as e:
            raise RPCError(str(e))

        if r.status_code == 401:
            raise RPCError("HTTP 401 - rpcuser/rpcpassword mismatch", 401)
        if r.status_code == 403:
            raise RPCError("HTTP 403 - rpcallowip not set in bitcoinz.conf", 403)

        try:
            data = r.json()
        except ValueError:
            raise RPCError(f"Non-JSON response: {r.text[:200]}")

        err = data.get("error")
        if err:
            code = err.get("code", 0) if isinstance(err, dict) else 0
            msg  = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RPCError(msg, code)

        return data.get("result")

    def getInfo(self):             return self.call("getinfo", [])
    def getBlockchainInfo(self):   return self.call("getblockchaininfo", [])
    def getNetworkInfo(self):      return self.call("getnetworkinfo", [])
    def getWalletInfo(self):       return self.call("getwalletinfo", [])
    def getPeerInfo(self):         return self.call("getpeerinfo", [])
    def getConnectionCount(self):   return self.call("getconnectioncount", [])
    def getUnconfirmedBalance(self): return self.call("getunconfirmedbalance", [])
    def getNewAddress(self) -> str:   return self.call("getnewaddress", [])
    def z_getNewAddress(self) -> str: return self.call("z_getnewaddress", [])
    def validateAddress(self, address: str) -> dict:
        return self.call("validateaddress", [address])
    def z_validateAddress(self, address: str) -> dict:
        return self.call("z_validateaddress", [address])

    def ListAddresses(self) -> list:
        try:
            result = self.call("listaddresses", [])
            if isinstance(result, list):
                return result
        except RPCError:
            pass
        try:
            rows = self.call("listreceivedbyaddress", [0, True])
            return [r["address"] for r in rows]
        except RPCError:
            pass
        return []

    def z_listAddresses(self) -> list:  return self.call("z_listaddresses", [])
    def z_getTotalBalance(self) -> dict: return self.call("z_gettotalbalance", [0])
    def z_getBalance(self, address: str, minconf: int = 0) -> float:
        return float(self.call("z_getbalance", [address, int(minconf)]))

    def z_listUnspent(self, address: str, minconf: int = 0, maxconf: int = 9999999) -> list:
        return self.call("z_listunspent", [minconf, maxconf, True, [address]])

    def z_listReceivedByAddress(self, address: str, minconf: int = 0) -> list:
        return self.call("z_listreceivedbyaddress", [address, int(minconf)])

    def listTransactions(self, count: int, tx_from: int) -> list:
        return self.call("listtransactions", ["*", count, tx_from])

    def z_sendMany(self, uaddress: str, toaddress: str, amount: float, txfee: float) -> str:
        return self.call("z_sendmany", [
            uaddress,
            [{"address": toaddress, "amount": float(amount)}],
            1, float(txfee),
        ])

    def SendMemo(self, uaddress: str, toaddress: str, amount: float, txfee: float, memo: str) -> str:
        hex_memo = binascii.hexlify(memo.encode()).decode()
        return self.call("z_sendmany", [
            uaddress,
            [{"address": toaddress, "amount": float(amount), "memo": hex_memo}],
            1, float(txfee),
        ])

    def z_getOperationStatus(self, opid: str) -> list:
        return self.call("z_getoperationstatus", [[opid]])

    def z_getOperationResult(self, opid: str) -> list:
        return self.call("z_getoperationresult", [[opid]])

    def DumpPrivKey(self, address: str) -> str: return self.call("dumpprivkey", [address])
    def z_ExportKey(self, address: str) -> str:  return self.call("z_exportkey", [address])
    def z_ExportWallet(self, filename: str) -> str: return self.call("z_exportwallet", [filename], timeout=120)
    def z_ImportWallet(self, filepath: str) -> None: return self.call("z_importwallet", [filepath], timeout=7200)

    def walletPassphrase(self, passphrase: str, timeout_sec: int = 300) -> None:
        self.call("walletpassphrase", [passphrase, int(timeout_sec)])

    def ImportPrivKey(self, key: str, rescan: bool = True) -> None:
        self.call("importprivkey", [key, "", bool(rescan)], timeout=7200)

    def z_ImportKey(self, key: str, start_height: int = 0, rescan: str = "yes") -> dict:
        rescan_mode = str(rescan or "yes").lower()
        if rescan_mode not in {"yes", "no", "whenkeyisnew"}:
            rescan_mode = "yes"
        return self.call("z_importkey", [key, rescan_mode, int(start_height)], timeout=7200)

    def getTransaction(self, txid: str) -> dict:    return self.call("gettransaction", [txid])
    def getRawTransaction(self, txid: str) -> dict: return self.call("getrawtransaction", [txid, 1])
    def getBlock(self, blockhash: str) -> dict:     return self.call("getblock", [blockhash, 2])
    def z_viewTransaction(self, txid: str) -> dict: return self.call("z_viewtransaction", [txid])
    def stopNode(self):                             return self.call("stop", [])
