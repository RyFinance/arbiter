# Jurisdiction & compliance — Polymarket access for US and Canadian persons — 2026-08-15

**Status:** Complete draft for the arbitrage project. **As-of date:
2026-08-15.** All sources are cited with URLs and dates. Quoted text is
verbatim from the cited page; everything else is paraphrase.

## Bottom line

1. The international platform (polymarket.com) **blocks US persons and
   persons located in the US entirely**, and blocks trading from **four
   Canadian provinces** (Ontario, British Columbia, Alberta, Quebec).
2. The block is **IP-based geoblocking** with three enforcement tiers,
   reinforced by a VPN prohibition (Terms of Service §2.1.4), self-
   certification demands, and wallet-level close-only sanctions.
3. There is now a **separate CFTC-regulated US venue** — Polymarket US
   (QCX LLC, a designated contract market and derivatives clearing
   organization) — which is the only Polymarket-branded route for US
   persons, and it is currently subject to active state-level litigation
   in several states.
4. **For this project:** the operator machine's own egress IP resolves to
   **British Columbia, Canada**, which the live Polymarket geoblock API
   classifies as blocked (`{"blocked":true,"country":"CA","region":"BC"}`,
   verified 2026-08-15). Live order submission from the current operator
   location would violate the platform's close-only tier; **live trading
   must remain blocked** until the operator either (a) relocates the
   trading operation to a non-restricted jurisdiction with matching
   attestations, or (b) gains access through a regulated venue. Paper
   trading (no real orders) has been and remains the only compliant mode
   for this operator.

## 1. United States

### 1.1 Federal history: the 2022 CFTC order

- On **2022-01-03** the CFTC entered an order against
  **Blockratize, Inc. d/b/a Polymarket** (Delaware corporation based in
  New York) finding it had offered off-exchange event-based binary option
  contracts without designation as a designated contract market (DCM) or
  registration as a swap execution facility (SEF), in violation of the
  Commodity Exchange Act (CEA) and CFTC regulations.
- Sanctions: **$1.4 million civil monetary penalty**, cease and desist,
  and an obligation to "facilitate the resolution (i.e. wind down) of all
  markets displayed on Polymarket.com that do not comply with the CEA."
- The order states the event-market contracts "constitute swaps under the
  CFTC's jurisdiction, and therefore can only be offered on a registered
  exchange in accordance with the CEA and CFTC regulations."
- Source: CFTC Press Release 8478-22,
  https://www.cftc.gov/PressRoom/PressReleases/8478-22 (2022-01-03);
  order PDF linked from that release.

### 1.2 Current platform terms for US persons (international platform)

The ToS body at polymarket.com/tos is client-side rendered and its full
text is not archivable; the following is the platform's own user-facing
restrictive language (embedded app strings, help center, API docs):

- Trading attestation shown to users:
  > "By trading, you agree to the Terms of Use, including that you are not
  > (i) a U.S. person and (ii) located in the United States, France or
  > other restricted territory."
- Trading modal (non-US IP gate):
  > "…attest that you are not (i) a U.S. person; (ii) located in the
  > United States, France or other restricted jurisdiction; and (iii)
  > using a VPN or other measures to circumvent or attempt to circumvent
  > restrictions. Polymarket reserves the right to put your wallet in
  > close-only mode if you do not comply with these Terms."
- Help Center ("Geographic Restrictions", modified 2026-08-14):
  > "Polymarket strictly prohibits the use of VPNs or similar tools to
  > bypass geographic restrictions. Such actions are considered violations
  > of the platform's Terms of Service (Section 2.1.4)."
- Site footer (live 2026-08-15):
  > "Polymarket operates globally through separate legal entities.
  > Polymarket US is operated by QCX LLC d/b/a Polymarket US, a
  > CFTC-regulated Designated Contract Market. This international platform
  > is not regulated by the CFTC and operates independently."
- Sources: Wayback snapshot of polymarket.com/tos captured
  2026-08-15 00:06 UTC
  (https://web.archive.org/web/20260815000605id_/https://polymarket.com/tos);
  2022-08-05 snapshot carrying "Trading is not available to Restricted
  Persons, including US Persons";
  https://help.polymarket.com/en/articles/13364163-geographic-restrictions;
  https://polymarket.com/ (footer).

**State-by-state note:** Polymarket's own published terms do not vary by
US state on the international platform — the US restriction is uniform
(all states), enforced federally (CFTC order) and by platform policy.
State-by-state variability exists instead at the **regulated venues**
(polymarket.us / Kalshi), where state gaming regulators have moved
independently (see 1.4).

### 1.3 The blocking mechanism

- **IP geoblock with region granularity.** The documented endpoint
  `GET https://polymarket.com/api/geoblock` returns
  `{blocked, ip, country, region}`. Three tiers per the API docs
  (https://docs.polymarket.com/api-reference/geoblock):
  - OFAC-sanctioned jurisdictions: **block completely** (no new orders;
    positions cannot be closed);
  - regulatory-restricted: **close-only on frontend and API** — this tier
    includes **United States (US)** and **Canada — British Columbia /
    Ontario / Alberta / Quebec**;
  - frontend-only close-only: IE, JP, MT (sports only), NL.
  - "Orders submitted from blocked regions will be rejected."
- **Functional disabling:** distinct app states for "Trading / Depositing /
  Withdrawals are not available in your region."
- **VPN prohibition + wallet sanctions:** VPN use is a ToS violation
  (§2.1.4 per the Help Center); the app warns Polymarket "reserves the
  right to put your wallet in close-only mode."
- **Self-certification:** users flagged by geolocation must certify they
  are "not a US resident or located in the United States or any restricted
  region," with a 14-day response deadline before close-only mode.
- **Location-based, not residency-based:** trading is permitted while
  physically in a non-restricted country (Help Center).
- **Perps:** order placement is not permitted from the United States,
  Canada, Cuba, Iran, North Korea, Syria, Crimea, Donetsk, Luhansk
  (https://docs.polymarket.com/api-reference/perps/geographic-restrictions).
- **Live verification from this project's machine (2026-08-15):**
  `curl https://polymarket.com/api/geoblock` →
  `{"blocked":true,"country":"CA","region":"BC"}`. The operator's own IP is
  in the close-only tier.

### 1.4 Polymarket US (QCX LLC) and state-by-state variability

- **Entity/status:** "Polymarket US is operated by QCX LLC d/b/a Polymarket
  US," a **CFTC-regulated Designated Contract Market (DCM) and Derivatives
  Clearing Organization (DCO)**; fiat (USD) venue.
  Sources: polymarket.com footer (live 2026-08-15);
  https://docs.polymarket.us/getting-started/what-is-polymarket-us.md;
  https://docs.polymarket.us/partners/platform-model.md.
- **Access timeline:** Polymarket blocked US customers on the international
  platform from 2022; CFTC granted an **Amended Order of Designation** in
  November 2025 enabling intermediated US market access
  (PR Newswire 2025-11-25; CoinDesk 2025-11-25;
  https://en.wikipedia.org/wiki/Polymarket, revision fetched
  2026-08-15). Polymarket announced the **$112M acquisition of QCEX** (a
  CFTC-licensed exchange/clearinghouse) in July 2025.
- **Products:** "Polymarket US currently offers markets on: NFL, NBA, NHL,
  MLB, MLS, CBB, Tennis, golf, and more. Politics, culture, finance, and
  economics coming soon." (docs.polymarket.us, as of 2026-08-15).
- **No state-restriction list published by Polymarket US itself** (its
  trading-restrictions page covers employee/MNPI rules, not states).
  State-level constraints come from regulators and courts:

| State | Actor | Action | Date | Source |
| --- | --- | --- | --- | --- |
| Nevada | NV Gaming Control Board | C&D to KalshiEX LLC: sports/election event contracts "unlawful in Nevada"; compliance by 2025-03-14 | 2025-03-04 | gaming.nv.gov press release PDF; sportico.com 2025-03-05 |
| Nevada | NGCB | Civil complaint against Polymarket seeking to bar event contracts without a gaming license; preliminary injunction granted | Jan 2026; injunction 2026-05-29 | en.wikipedia.org/wiki/Polymarket (secondary); rg.org 2026-06-03 |
| Massachusetts | Mass. Superior Court | Preliminary injunction in *Commonwealth v. KalshiEX LLC*: event contracts = illegal sports wagering | early 2026 | en.wikipedia.org (secondary) |
| Michigan | MI Gaming Control Board | Investigation of sports event contracts | 2025-04-11 | sbcamericas.com |
| Michigan | Ingham County Circuit Court | TRO barring KalshiEX sports contracts to MI residents (fines up to $120k/day) | 2026-06-29/30 | michigan.gov/mgcb |
| Minnesota | Legislature / DOJ | Law banning prediction markets, eff. 2026-08-01; DOJ sued next day | May–Aug 2026 | en.wikipedia.org (secondary) |
| New Jersey | NJ regulators vs Kalshi | Third Circuit: NJ cannot enforce state law against Kalshi's prediction market | 2026-04-06 | reuters.com |
| Tennessee | State regulators | C&D; federal TRO blocked enforcement of state gaming laws | Jan–Feb 2026 | natlawreview.com |
| Ohio | Casino Control Commission | C&D to Kalshi; Kalshi suing the Commission | Oct 2025 | igamingbusiness.com |
| Arizona, Connecticut, Illinois | CFTC & DOJ (federal) | Federal suits against the three states over prediction-market regulation | Apr 2026 | forbes.com 2026-04-09 |

### 1.5 Federal rulemaking in motion

- 2024-06-10: CFTC NPRM "Event Contracts."
- 2025-09-30: CFTC Staff Advisory on sports event contracts (Staff
  Letter 25-36).
- **2026-02-04: CFTC withdrew the 2024 NPRM and the sports advisory**
  (CFTC PR 9179-26; 91 Fed. Reg. 2026-02454).
- 2026-03-16: ANPRM on "Prediction Markets" (Fed. Reg. 2026-05105).
- 2026-06-10: NPRM amending CFTC Reg 40.11 with new Appendix F
  (wilmerhale.com client alert 2026-06-18; CRS Legal Sidebar LSB11441).

## 2. Canada

### 2.1 The OSC proceeding and settlement

- **Ontario Securities Commission v. Blockratize Inc. and Adventure One
  QSS Inc., Capital Markets Tribunal File No. 2025-8.**
- Allegation: between **June 2020 and May 2023**, Polymarket's operators
  "violated Ontario securities law by offering binary options to investors
  in Ontario through their online global options trading platform
  Polymarket in breach of Multilateral Instrument 91-102 Prohibition of
  Binary Options" (OSC news release 2025-04-17).
- Settlement agreement dated **2025-03-31**; Tribunal hearing
  **2025-04-14**; order approving settlement **2025-04-17**.
- Sanctions: **two-year market bans** (including bans on trading
  securities or derivatives) on Blockratize and Adventure One QSS;
  **$200,000 administrative penalty**; **US$22,966.75** voluntary
  payment; **$25,000** toward investigation costs.
- Sources:
  https://www.osc.ca/en/news-events/news/osc-reaches-settlement-current-and-former-operators-polymarket-breach-binary-options-ban;
  https://www.capitalmarketstribunal.ca/en/notices/notice-ontario-securities-commission-v-blockratize-inc-file-no-2025-8-0;
  settlement PDF at capitalmarketstribunal.ca (2025-04).

### 2.2 Current Canadian blocking status

- **Ontario:** blocked since **mid-2023** (Polymarket geo-blocked Ontario
  residents, allowing only close-outs; formalized by the 2025 settlement,
  with the geo-block **maintained and annually certified** as a settlement
  condition per third-party reporting).
- Help Center (modified 2026-08-14):
  > "Ontario, Canada: Trading services are unavailable. Users can access
  > market data globally but cannot deposit or trade, including via the
  > iOS app."
- **Alberta, British Columbia, Quebec:** Polymarket voluntarily exited
  these three provinces in **July 2026**, around Alberta's iGaming launch
  (casinos.com 2026-07-15; canadiangamingbusiness.com 2026-07-13). The
  current Help Center lists blocked regions "Canada (CA): Alberta (AB),
  British Columbia (BC), Ontario (ON), Quebec (QC)."
- **Other provinces:** no enforcement action found from the AMF (Quebec)
  or BCSC against Polymarket (tradetheoutcome.com, Feb 2026: "Ontario is
  the only Canadian province to have taken formal regulatory action
  against Polymarket"). Access from other provinces is not currently
  blocked by Polymarket policy, but this operator's location (BC) is.
- CBC (2026-01-10): "Polymarket lists Ontario as a banned region — but no
  other parts of Canada, and VPNs can be used to get around such
  restrictions (though that's against Polymarket's terms)."
  https://www.cbc.ca/news/business/prediction-markets-maduro-bets-9.7038860

## 3. What would need to be true before live trading could be considered

For this project, in priority order:

1. **Operator location.** The operator machine is physically in British
   Columbia (live geoblock API test above). BC is in the close-only tier.
   Live order submission from the current location violates the platform's
   terms and exposes funds to wallet close-only sanctions. Either the
   trading operation must move to a non-restricted jurisdiction (with the
   operator physically present there — the restriction is location-based,
   not residency-based), or trading must go through a regulated venue.
   **VPN circumvention is explicitly prohibited (ToS §2.1.4) and is not
   an acceptable path.**
2. **Regulated US path.** US-person trading would require using the CFTC
   DCM (Polymarket US / QCX LLC), whose current product set is sports
   contracts (politics/finance "coming soon"), not the BTC up/down
   binaries this project trades on the international platform. Access
   would also need to survive the active state litigation (Nevada
   injunction, Michigan TRO, Minnesota ban, Massachusetts PI) and the
   unsettled federal rulemaking (Reg 40.11 NPRM, 2026-06-10).
3. **Canadian path.** For the international platform: operator relocation
   to a non-restricted jurisdiction + the platform's attestations (not a
   US person; not located in a restricted territory; no VPN). The OSC's
   two-year market bans on the Polymarket operators run until at least
   April 2027 and require annual geo-block certification; the OSC action
   itself shows Ontario will treat binary-options offerings to Ontario
   residents as breaches of MI 91-102.
4. **Project-level gates.** Even with a compliant operator location, this
   project's own go/no-go gates remain blocking: fill fidelity, quote
   freshness, PnL convergence, latency, depth continuity, and the BTC
   model economics are all failed or unproven (see
   [final_status_2026-08-15.md](final_status_2026-08-15.md)). Compliance
   is necessary but not sufficient.

**Conclusion for today:** no live trading. Real orders submitted to date:
`0`. Paper-only sessions with the geoblock endpoint confirmed blocked
(`CA/BC`) are the compliant operating mode, and the paper path itself
must continue to avoid any order submission.

## 4. Sources

Primary:

1. CFTC PR 8478-22 (2022-01-03) — https://www.cftc.gov/PressRoom/PressReleases/8478-22
2. Polymarket Help Center, Geographic Restrictions (mod. 2026-08-14) — https://help.polymarket.com/en/articles/13364163-geographic-restrictions
3. Polymarket API docs, geoblock — https://docs.polymarket.com/api-reference/geoblock
4. Polymarket Perps docs, geographic restrictions — https://docs.polymarket.com/api-reference/perps/geographic-restrictions
5. Polymarket ToS wayback captures — https://web.archive.org/web/20260815000605id_/https://polymarket.com/tos (2026-08-15); https://web.archive.org/web/20220805201309/https://polymarket.com/tos (2022-08-05)
6. polymarket.com footer disclaimer (live 2026-08-15) — https://polymarket.com/
7. Polymarket US docs — https://docs.polymarket.us/getting-started/what-is-polymarket-us.md; /partners/platform-model.md; /learn/trading/access-and-limits/trading-restrictions.md
8. OSC news release (2025-04-17) — https://www.osc.ca/en/news-events/news/osc-reaches-settlement-current-and-former-operators-polymarket-breach-binary-options-ban
9. CMT notice, File 2025-8 — https://www.capitalmarketstribunal.ca/en/notices/notice-ontario-securities-commission-v-blockratize-inc-file-no-2025-8-0
10. Live geoblock endpoint test (2026-08-15) — https://polymarket.com/api/geoblock → {"blocked":true,"country":"CA","region":"BC"}
11. CFTC PR 9179-26 (2026-02-04) — https://www.cftc.gov/PressRoom/PressReleases/9179-26
12. Fed. Reg. 2026-05105, Prediction Markets ANPRM (2026-03-16) — https://www.federalregister.gov/documents/2026/03/16/2026-05105/prediction-markets
13. CRS Legal Sidebar LSB11441 — https://www.congress.gov/crs_external_products/LSB/PDF/LSB11441/LSB11441.2.pdf

News and secondary:

14. Reuters, NJ v. Kalshi (2026-04-06) — https://www.reuters.com/world/new-jersey-cannot-regulate-kalshis-prediction-market-us-appeals-court-rules-2026-04-06/
15. Michigan MGCB TRO reaction (2026-06-30) — https://www.michigan.gov/mgcb/news/2026/06/30/mgcb-reacts-to-temporary-restraining-order-against-kalshiex
16. CNN, Michigan federal dispute (2026-07-17) — https://edition.cnn.com/2026/07/17/politics/trump-prediction-markets-michigan-kalshi
17. National Law Review, prediction markets v. state gaming laws (2026-02-12) — https://natlawreview.com/article/prediction-markets-v-state-gaming-laws-kalshi-litigation-gamble
18. Forbes, CFTC/DOJ suits v. AZ/CT/IL (2026-04-09) — https://www.forbes.com/sites/jasonbrett/2026/04/09/prediction-market-regulator-sues-3-states-as-kalshi-wins-in-new-jersey/
19. NGCB Kalshi C&D (2025-03-04) — https://www.gaming.nv.gov/siteassets/content/about/press-release/NGCB_-_News_Release_-_KalshiEX_Cease_and_Desist_Order__3-4-2025.pdf
20. Sportico (2025-03-05) — https://www.sportico.com/business/sports-betting/2025/nevada-order-kalshi-sports-futures-contracts-1234841960/
21. iGaming Business, Ohio (2025-10-09) — https://igamingbusiness.com/legal-compliance/kalshi-ohio-prediction-markets-lawsuit-october/
22. casinos.com, Polymarket leaves 3 provinces (2026-07-15) — https://www.casinos.com/ca/news/polymarket-leaves-alberta-two-other-canadian-provinces-before-igaming-launch
23. Canadian Gaming Business (2026-07-13) — https://www.canadiangamingbusiness.com/2026/07/13/polymarket-blocks-provinces/
24. CBC (2026-01-10) — https://www.cbc.ca/news/business/prediction-markets-maduro-bets-9.7038860
25. bitsofblocks.io (2025-04-03) — https://www.bitsofblocks.io/post/canadian-regulator-win-bet-against-polymarket-with-settlement-of-enforcement-proceedings
26. Wikipedia, "Polymarket" (secondary; NV/MN/MA rows) — https://en.wikipedia.org/wiki/Polymarket
27. PR Newswire (2025-11-25) and CoinDesk (2025-11-25), Amended Order of Designation coverage (URLs not resolvable from news indexes at fetch time; cited via Wikipedia/aggregators)
28. WilmerHale client alert (2026-06-18) — https://www.wilmerhale.com/en/insights/client-alerts/20260618-10-takeaways-from-the-cftc-event-contracts-proposed-rulemaking

**Caveats:** rows marked "(secondary)" rest on Wikipedia's summary because
direct primary pages were not retrievable in this research session; the
ToS body text itself is client-side rendered and was quoted only via the
platform's own modal/attestation/help strings as noted inline.
