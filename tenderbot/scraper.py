"""Deep scraper for the Bihar eProcurement portal (eproc2.bihar.gov.in).

How the portal works (checked on the live site, Oct 2026):
* The "Latest Tenders" table only *shows* 20 rows, but the page's Angular controller
  (`openareaTenderList`) already holds the FULL list of active tenders in `allTenderList`
  (~700 tenders across ~55 departments), plus the department list (`orgList`) and the
  category/tender-type masters. So one page load gives every active tender.
* The "View" pop-up loads `/rest/quotation/previewTenderByTenderId`, which carries the
  estimated value, EMD, tender fee and pre-bid meeting. The portal refuses these calls from
  outside a browser, so we make them from inside the loaded page, through the page's own
  Angular `$http`, one at a time with a pause, and only for tenders we have not seen before.
"""
import logging

from . import config
from .normalize import normalize_listing, summarize_detail

log = logging.getLogger(__name__)

_LISTING_JS = """() => {
  let el = null;
  document.querySelectorAll('[ng-controller]').forEach(e => {
    if (e.getAttribute('ng-controller') === 'openareaTenderList') el = e;
  });
  if (!el || !window.angular) return null;
  const sc = angular.element(el).scope();
  if (!sc || !sc.allTenderList || !sc.allTenderList.length) return null;
  const keep = ['currentOrgTenderId','currenttenderid','currenttenderrefno','currentdescription',
    'currentTenderPublishDate','currentbidStartDate','currentbidEndDate','currentbidOpenDate',
    'currentdeptid','currentorgid','currentproccatid','currenttendertypeid','currenttendercatid','currentstatus'];
  const ct = sc.categoryTypeList || {};
  return {
    tenders: sc.allTenderList.map(t => Object.fromEntries(keep.map(k => [k, t[k]]))),
    orgs: (sc.orgList || []).map(o => ({organizationId: o.organizationId,
            organizationName: o.organizationName, organizationCode: o.organizationCode})),
    procCats: (ct.procCatList || []).map(p => [p.proccatId, p.description]),
    tenderTypes: (ct.rfqTypeList || []).map(p => [p.rfqtypeId, p.description]),
  };
}"""

_DETAIL_JS = """async ([ids, delayMs]) => {
  let el = null;
  document.querySelectorAll('[ng-controller]').forEach(e => {
    if (e.getAttribute('ng-controller') === 'openareaTenderList') el = e;
  });
  const $http = angular.element(el).injector().get('$http');
  const out = {};
  for (const id of ids) {
    try {
      const r = await $http.post(contextPath + '/rest/quotation/previewTenderByTenderId?tenderId=' + id);
      const d = r.data || {};
      // keep only what we parse; drop signatures, certificates, BOQ rows etc.
      const pm = d.tenderPreviewMap || {};
      out[id] = {
        pacamt: d.pacamt, pacVisibilityFlag: d.pacVisibilityFlag, queryString: d.queryString,
        orgid: d.orgid, offerValidity: d.offerValidity,
        office: (d.tenderIssuingAuthority || {}).tenderIssuingAuthorityName || null,
        officer: (pm.creatorDetail || {}).DealingOfficer || null,
        templates: (d.templates || []).filter(tp => tp.subProcessName !== 'BOQ').map(tp => ({
          subProcessName: tp.subProcessName, templategroupId: tp.templategroupId,
          templateFieldList: (tp.templateFieldList || []).filter(f => f.value !== null && f.value !== '')
              .map(f => ({code: f.code, value: String(f.value).slice(0, 3000)}))
        }))
      };
    } catch (e) {
      out[id] = {error: (e && e.status) || String(e)};
    }
    await new Promise(r => setTimeout(r, delayMs));
  }
  return out;
}"""


class PortalScraper:
    """Use as a context manager: opens the portal once, then lists and fetches details."""

    def __init__(self, headless=None, timeout_ms=150_000):
        self.headless = config.HEADLESS if headless is None else headless
        self.timeout_ms = timeout_ms

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(ignore_https_errors=True, locale="en-IN",
                                        timezone_id="Asia/Kolkata")
        self.page = ctx.new_page()
        self.page.goto(config.PORTAL_URL, wait_until="networkidle", timeout=self.timeout_ms)
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def listing(self):
        data = None
        for _ in range(40):  # Angular fills the list asynchronously
            data = self.page.evaluate(_LISTING_JS)
            if data:
                break
            self.page.wait_for_timeout(1500)
        if not data:
            raise RuntimeError("Tender list did not load (portal layout may have changed).")
        return parse_listing(data)

    def details(self, tender_ids, batch=20):
        results = {}
        for i in range(0, len(tender_ids), batch):
            chunk = tender_ids[i:i + batch]
            raw = self.page.evaluate(_DETAIL_JS, [chunk, config.DETAIL_DELAY_MS])
            for k, v in raw.items():
                if "error" in v:
                    log.warning("detail %s failed: %s", k, v["error"])
                    continue
                results[int(k)] = summarize_detail(v)
            log.info("details %d/%d", min(i + batch, len(tender_ids)), len(tender_ids))
        return results


def parse_listing(data):
    """Raw page data -> (tenders, orgs). Separated out so it can be tested without a browser."""
    orgs = {o["organizationId"]: o for o in data["orgs"]}
    proc_cats = {k: v for k, v in data.get("procCats", [])}
    tender_types = {k: v for k, v in data.get("tenderTypes", [])}
    seen, tenders = set(), []
    for t in data["tenders"]:
        if not t.get("currenttenderid") or t["currenttenderid"] in seen:
            continue
        seen.add(t["currenttenderid"])
        tenders.append(normalize_listing(t, orgs, proc_cats, tender_types))
    return tenders, data["orgs"]
