"""One-shot: apply the approved tickers/authors/themes from the curated review.

Idempotent — re-running is safe. Skips entries already present.
"""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
COMPANIES_PATH = PROJECT_ROOT / "config" / "companies.json"


# --- Approved tickers -------------------------------------------------------

US_TICKERS = [
    ("AAOI",  "Applied Optoelectronics"),
    ("ACLS",  "Axcelis Technologies"),
    ("AEHR",  "Aehr Test Systems"),
    ("AEP",   "American Electric Power"),
    ("APH",   "Amphenol Corp"),
    ("ASTS",  "AST SpaceMobile"),
    ("AUR",   "Aurora Innovation"),
    ("AXTI",  "AXT Inc"),
    ("BE",    "Bloom Energy"),
    ("BKSY",  "BlackSky Technology"),
    ("CACI",  "CACI International"),
    ("CBRS",  "Cerebras Systems"),
    ("CDNS",  "Cadence Design Systems"),
    ("CLF",   "Cleveland-Cliffs"),
    ("CLS",   "Celestica"),
    ("COHU",  "Cohu Inc"),
    ("CRDO",  "Credo Technology Group"),
    ("CRWV",  "CoreWeave"),
    ("CSCO",  "Cisco Systems"),
    ("DY",    "Dycom Industries"),
    ("ENTG",  "Entegris"),
    ("EQIX",  "Equinix"),
    ("ETN",   "Eaton Corporation"),
    ("FCEL",  "FuelCell Energy"),
    ("FLEX",  "Flex Ltd"),
    ("FN",    "Fabrinet"),
    ("FYBR",  "Frontier Communications"),
    ("GEV",   "GE Vernova"),
    ("GSAT",  "Globalstar"),
    ("ICHR",  "Ichor Holdings"),
    ("IONQ",  "IonQ"),
    ("JBL",   "Jabil Inc"),
    ("KEYS",  "Keysight Technologies"),
    ("LPTH",  "LightPath Technologies"),
    ("LUMN",  "Lumen Technologies"),
    ("LWLG",  "Lightwave Logic"),
    ("MBLY",  "Mobileye Global"),
    ("MCHP",  "Microchip Technology"),
    ("MKSI",  "MKS Instruments"),
    ("MP",    "MP Materials"),
    ("MRVL",  "Marvell Technology"),
    ("MTSI",  "MACOM Technology Solutions"),
    ("MXL",   "MaxLinear"),
    ("NBIS",  "Nebius Group"),
    ("NEE",   "NextEra Energy"),
    ("NOVT",  "Novanta"),
    ("NTAP",  "NetApp"),
    ("NVMI",  "Nova Ltd"),
    ("NVTS",  "Navitas Semiconductor"),
    ("ON",    "ON Semiconductor"),
    ("ONTO",  "Onto Innovation"),
    ("POET",  "POET Technologies"),
    ("POWI",  "Power Integrations"),
    ("PSTG",  "Pure Storage"),
    ("QBTS",  "D-Wave Quantum"),
    ("RGTI",  "Rigetti Computing"),
    ("RKLB",  "Rocket Lab USA"),
    ("SANM",  "Sanmina Corp"),
    ("SITM",  "SiTime Corporation"),
    ("SMTC",  "Semtech Corp"),
    ("SPCX",  "SpaceX"),
    ("TEL",   "TE Connectivity"),
    ("TSEM",  "Tower Semiconductor"),
    ("TXN",   "Texas Instruments"),
    ("UCTT",  "Ultra Clean Holdings"),
    ("VIAV",  "Viavi Solutions"),
    ("WRD",   "WeRide"),
]

ASIA_EU_TICKERS = [
    ("AWX SP",   "AEM Holdings",                  "SG"),
    ("558 SP",   "UMS Integration",               "SG"),
    ("ATS AV",   "AT&S Austria",                  "EU"),
    ("402340 KS","SK Square",                     "KR"),
    ("1802 TT",  "Taiwan Glass Industry",         "TW"),
    ("6173 TT",  "Prosperity Dielectrics",        "TW"),
    ("6278 JT",  "Union Tool",                    "JP"),
    ("6368 JT",  "Organo Corporation",            "JP"),
    ("6370 JT",  "Kurita Water Industries",       "JP"),
    ("6492 JT",  "Okano Valve Mfg",               "JP"),
    ("6728 JT",  "Ulvac Inc",                     "JP"),
    ("6855 JT",  "Japan Electronic Materials",    "JP"),
]


# --- Approved authors -------------------------------------------------------

MACRO_AUTHORS = [
    "Warren Pies (3Fourteen Research)",
    "Philip Swift",
    "Luke Gromen",
    "Jordi Visser (22V Research)",
    "Dennis DeBusschere (22V Research)",
    "Kim Wallace (22V Research)",
    "Peter Williams (22V Research)",
    "Art Berman",
    "Jan van Eck",
    "Jason Shapiro (Crowded Market Report)",
    "Josh Brown",
    "Michael Batnick",
]

SEMIS_AUTHORS = [
    "SemiAnalysis",
    "Chipstrat",
    "Tessara",
    "FundaAI",
    "Damnang",
    "PhotonCap",
    "Gaetano",
    "Tae Kim",
    "Vikram Sekar",
    "Semi Doped",
    "Jason's Chips",
    "Irrational Analysis",
    "Collyer Bridge",
    "NuttyCLD",
    "Asymmetrical Bets",
    "quantLR",
    "Ben Thompson (Stratechery)",
    "Gavin Baker",
]


# --- Approved themes --------------------------------------------------------

NEW_THEMES = [
    "AI Infrastructure",
    "Agentic AI",
    "EDA",
    "AI Inference",
    "Fuel Cells",
    "Data Center Power",
    "Quantum Computing",
]


# ---------------------------------------------------------------------------

def apply_all() -> None:
    print("Applying approved additions...")
    print()

    # 1. Tickers
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)

    added_us, added_intl, skipped = [], [], []
    for ticker, name in US_TICKERS:
        if ticker in companies:
            skipped.append(ticker)
            continue
        companies[ticker] = {"name": name, "market": "US"}
        added_us.append(ticker)
    for ticker, name, market in ASIA_EU_TICKERS:
        if ticker in companies:
            skipped.append(ticker)
            continue
        companies[ticker] = {"name": name, "market": market}
        added_intl.append(ticker)
    with open(COMPANIES_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Tickers: +{len(added_us)} US, +{len(added_intl)} Asia/EU, {len(skipped)} already present")
    if added_us:
        print(f"  US: {', '.join(added_us)}")
    if added_intl:
        print(f"  Intl: {', '.join(added_intl)}")
    if skipped:
        print(f"  Skipped (dupes): {', '.join(skipped)}")

    # 2. Macro authors
    macro_added = []
    macro_base = DATA_DIR / "Macro" / "authors"
    for author in MACRO_AUTHORS:
        d = macro_base / author / "notes"
        if d.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        macro_added.append(author)
    print(f"\nMacro authors: +{len(macro_added)}")
    for a in macro_added:
        print(f"  + {a}")

    # 3. Semis authors
    semis_added = []
    semis_base = DATA_DIR / "Semis" / "authors"
    semis_base.mkdir(parents=True, exist_ok=True)
    for author in SEMIS_AUTHORS:
        d = semis_base / author / "notes"
        if d.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        semis_added.append(author)
    print(f"\nSemis authors: +{len(semis_added)}  (new category at data/Semis/)")
    for a in semis_added:
        print(f"  + {a}")

    # 4. Themes
    theme_base = DATA_DIR / "Thematic"
    theme_added = []
    for theme in NEW_THEMES:
        d = theme_base / theme / "notes"
        if d.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        (theme_base / theme / "analyses").mkdir(parents=True, exist_ok=True)
        theme_added.append(theme)
    print(f"\nThemes: +{len(theme_added)}")
    for t in theme_added:
        print(f"  + {t}")

    print(f"\nTotal new tickers in companies.json: {len(companies)}")
    print("Done.")


if __name__ == "__main__":
    apply_all()
