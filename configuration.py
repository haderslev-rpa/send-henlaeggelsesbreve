from __future__ import annotations
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# ------------------------------------------------------------
# DATO
# ------------------------------------------------------------

LOCAL_TIMEZONE = ZoneInfo("Europe/Copenhagen")

CURRENT_YEAR: int = datetime.now(
    tz=LOCAL_TIMEZONE,
).year


# ------------------------------------------------------------
# BROWSER
# ------------------------------------------------------------

HEADLESS: bool = True
DEBUG_HEADLESS: bool = False
QUEUE_HEADLESS: bool = True
USE_REGULAR_CHROMIUM: bool = True
PLAYWRIGHT_SKIP_BROWSER_GC: bool = True

# Navigation til den konkrete skade. Eksisterende ventelogik er bevaret.
INSUBIZ_BASE_URL: str = "https://start.insubiz.dk"
NAVIGATION_TIMEOUT_MS: int = 30_000
UI_WAIT_MS: int = 1_500


# ------------------------------------------------------------
# SKADELISTE
# ------------------------------------------------------------

CUSTOMER_ID: int | None = None

CUSTOMER_SEGMENTATION_1: int = -1
CUSTOMER_SEGMENTATION_2: int = -1

CLAIM_GROUP_ID: int = 0

STATUS_ID: int = -2

CREATED_YEAR_FROM: int = 0
CREATED_YEAR_TO: int = CURRENT_YEAR

INCIDENT_YEAR_FROM: int = CURRENT_YEAR - 1
INCIDENT_YEAR_TO: int = CURRENT_YEAR

SHOW_TREE_DATA: bool = False

SKADELISTE_COLUMNS: list[str] = [
    "Id",
    "IncidentNumberInternal",
    "IncidentType",
    "IncidentSubType",
    "IncidentStatus",
    "Created",
    "standardCase",
]


# ------------------------------------------------------------
# SKADEFILTRE
# ------------------------------------------------------------

STATUSSER_DER_SKAL_FJERNES: tuple[str, ...] = (
    "Afsluttet",
    "Genoptaget",
)

INCIDENT_SUBTYPE_DER_SKAL_BEHOLDES: str = "Arbejdsulykke"

INCIDENT_TYPE_DER_SKAL_BEHOLDES: str = "Arbejdsskade"

# Kun detailværdien True må komme i køen.

STANDARD_CASE_SKAL_VAERE: bool = True


# ------------------------------------------------------------
# DOKUMENT FRA SKABELON
# ------------------------------------------------------------

SKABELON_NAVN: str = "Robot - Henlæggelsesbrev"

DOKUMENTTITEL: str = "Brev fra Haderslev Kommune"

HOVEDDOKUMENT_NAVN: str = SKABELON_NAVN

# ------------------------------------------------------------
# DOKUMENTVALG EFTER STATE 2.0
# ------------------------------------------------------------

DOKUMENT_OPRETTET_AF: str = "ForsikringsService"
DOKUMENT_DIRECTORY_ID: int = 0
DOKUMENT_TIDSFORSKEL_SEKUNDER: int = 1

# ------------------------------------------------------------
# DIGITAL POST
# ------------------------------------------------------------

FORSENDELSESTYPE: str = "Insubizbrev"

BILAG_NAVN: str = "Anmeldelse af arbejdsulykke"

# True:
# Digital Post-dialogen udfyldes, men Send-knappen klikkes ikke.
#
# False:
# Digital Post sendes reelt.

DIGITAL_POST_TEST: bool = False

# ------------------------------------------------------------
# KOMMENTAR EFTER DIGITAL POST
# ------------------------------------------------------------
KOMMENTAR_TIDSZONE = LOCAL_TIMEZONE
KOMMENTAR_TITEL_PREFIX = "Henlæggelsesbrev afsendt til borger d. "
KOMMENTAR_DATOFORMAT = "%d-%m-%Y - kl: %H.%M"
KOMMENTAR_HOVEDDOKUMENT_LABEL = "Hoved dokument"
KOMMENTAR_BILAG_LABEL = "Bilag"


# ------------------------------------------------------------
# AUTOMATION SERVER
# ------------------------------------------------------------

QUEUE_ID: int = 17


# ------------------------------------------------------------
# MAIL VED FEJLET DIGITAL POST
# ------------------------------------------------------------

MAIL_AFSENDER = os.getenv("MAIL_AFSENDER", "").strip()

MAIL_MODTAGER_SEND_FEJLEDE = "forsikring@haderslev.dk"

MAIL_EMNE_SEND_FEJLEDE = "Digital Post fejlede - skade nr. {skade_nr}"

MAIL_TEKST_SEND_FEJLEDE = (
    "Borgeren har muligvis ikke har digital post, "
    "da der opstod et problem under afsendelsen af "
    "henlæggelsesbrevet.\n\n"
    "Skade nr.: {skade_nr}"
)

DIGITAL_POST_FEJL_POPUP_TITEL = "Send fejlede"

DIGITAL_POST_FEJL_POPUP_TIMEOUT_MS = 3_000