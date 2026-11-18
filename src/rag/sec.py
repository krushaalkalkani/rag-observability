"""Tiny SEC EDGAR client: find a company's 10-K filings and download them.

EDGAR fair-access rules: identify yourself with a User-Agent (name + email)
and stay under 10 requests/second. We enforce both here.
"""

import time

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"


class SecClient:
    def __init__(self, user_agent: str, max_requests_per_second: float = 5):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = 1.0 / max_requests_per_second
        self._last_request = 0.0
        self._ticker_to_cik: dict[str, int] | None = None

    def get(self, url: str) -> requests.Response:
        for attempt in range(4):
            # Rate limit: wait until enough time has passed since the last call.
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = self.session.get(url, timeout=60)
            if resp.status_code in (429, 503):  # throttled: back off and retry
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def cik_for(self, ticker: str) -> int:
        if self._ticker_to_cik is None:
            data = self.get(TICKERS_URL).json()
            self._ticker_to_cik = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
        return self._ticker_to_cik[ticker.upper()]

    def latest_10k_filings(self, ticker: str, n: int = 1) -> list[dict]:
        """Return metadata for the n most recent 10-K filings of a company."""
        cik = self.cik_for(ticker)
        subs = self.get(SUBMISSIONS_URL.format(cik=cik)).json()
        recent = subs["filings"]["recent"]
        filings = []
        for i, form in enumerate(recent["form"]):
            if form != "10-K":
                continue
            accession = recent["accessionNumber"][i]
            document = recent["primaryDocument"][i]
            filings.append(
                {
                    "ticker": ticker.upper(),
                    "company": subs["name"],
                    "cik": cik,
                    "form": form,
                    "accession": accession,
                    "filing_date": recent["filingDate"][i],
                    "report_date": recent["reportDate"][i],
                    # Fiscal year = year in which the reporting period ends.
                    "fiscal_year": int(recent["reportDate"][i][:4]),
                    "source_url": ARCHIVE_URL.format(
                        cik=cik, accession=accession.replace("-", ""), document=document
                    ),
                }
            )
            if len(filings) == n:
                break
        return filings
