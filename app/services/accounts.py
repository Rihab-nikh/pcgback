"""Chart-of-accounts resolution (Moroccan PCG).

One generic resolver replaces the six near-identical functions from the
monolith (_resolve_account, _tva_charge_account, _tva_asset_account,
_seller_tva_account, _fournisseur_account, _ras_account). Behavior is
preserved: try the semantic search service, reject weak-confidence
matches, fall back to hardcoded PCG defaults, cache everything.
"""
import httpx

from app.core.config import ACCOUNT_SEARCH_BASE_URL, ACCOUNTS_OFFLINE
from app.core.logging import logger
from app.core.db import query_one
from app.models.invoice import ExtractedInvoiceData

# Cache to avoid repeated lookups (process-lifetime)
_account_cache: dict[str, tuple[str, str]] = {}

# Fallback account mappings if search service is unavailable
ACCOUNT_FALLBACKS: dict[str, tuple[str, str]] = {
    "ventes de marchandises::71": ("7111", "Ventes de marchandises au Maroc"),
    "ventes de services prestations::71": ("7124", "Ventes de services produits au Maroc"),
    "achats de marchandises::61": ("6111", "Achats de marchandises"),
    "achats matieres premieres::61": ("6121", "Achats de matières premières"),
    "achats fournitures consommables::61": ("6122", "Achats de matières et fournitures consommables"),
    "achats non stockes::61": ("6125", "Achats non stockés de matières et fournitures"),
    "locations charges locatives::61": ("6131", "Locations et charges locatives"),
    "entretien reparations::61": ("6133", "Entretien et réparations"),
    "primes assurances::61": ("6134", "Primes d'assurances"),
    "honoraires intermediaires::61": ("61365", "Honoraires"),
    "etudes recherches documentation::61": ("6141", "Études, recherches et documentation"),
    "transports::61": ("6142", "Transports"),
    "publicite relations publiques::61": ("6144", "Publicité, publications et relations publiques"),
    "telecommunications::61": ("6145", "Frais postaux et frais de télécommunications"),
    "services bancaires::61": ("6147", "Services bancaires"),
    "redevances brevets::61": ("6137", "Redevances pour brevets, marques, droits et valeurs similaires"),
    # Safe temporary debit account: validation blocks posting until reclassified.
    "imputation a clarifier::34": ("3497", "Comptes transitoires ou d'attente — débiteurs"),
    "TVA recuperable charges::345": ("34552", "Etat — TVA récupérable (Charges)"),
    "TVA recuperable immobilisations::345": ("34551", "Etat — TVA récupérable (Immobilisations)"),
    "TVA facturee::44": ("4455", "Etat — TVA facturée"),
    "fournisseurs::44": ("4411", "Fournisseurs"),
    "retenue source::44": ("4452", "Etat — Retenue à la source"),
    # Immobilisations corporelles — exact Moroccan PCG families.
    "materiel informatique::23": ("2355", "Matériel informatique"),
    "mobilier de bureau::23": ("2351", "Mobilier de bureau"),
    "batiment immeuble::23": ("2321", "Bâtiments"),
    "vehicule automobile::23": ("2340", "Matériel de transport"),
    "installation equipement::23": ("2331", "Installations techniques"),
    "materiel outillage::23": ("2332", "Matériel et outillage"),
    "autres immobilisations corporelles::23": ("2380", "Autres immobilisations corporelles"),
}

RULE_KEY_BY_CACHE: dict[str, str] = {
    "ventes de marchandises::71": "sale.merchandise",
    "ventes de services prestations::71": "sale.service",
    "achats de marchandises::61": "purchase.merchandise",
    "achats matieres premieres::61": "purchase.raw_material",
    "achats fournitures consommables::61": "purchase.consumable_supplies",
    "achats non stockes::61": "purchase.nonstocked_supplies",
    "locations charges locatives::61": "purchase.rent",
    "entretien reparations::61": "purchase.maintenance",
    "primes assurances::61": "purchase.insurance",
    "honoraires intermediaires::61": "purchase.professional_fees",
    "etudes recherches documentation::61": "purchase.studies_documentation",
    "transports::61": "purchase.transport",
    "publicite relations publiques::61": "purchase.advertising",
    "telecommunications::61": "purchase.telecom",
    "services bancaires::61": "purchase.banking_services",
    "redevances brevets::61": "purchase.royalties",
    "materiel informatique::23": "asset.it",
    "mobilier de bureau::23": "asset.furniture",
    "batiment immeuble::23": "asset.building",
    "vehicule automobile::23": "asset.vehicle",
    "installation equipement::23": "asset.installation",
    "materiel outillage::23": "asset.equipment",
    "autres immobilisations corporelles::23": "asset.other",
    "TVA recuperable charges::345": "vat.input.expense",
    "TVA recuperable immobilisations::345": "vat.input.asset",
    "TVA facturee::44": "vat.output",
    "fournisseurs::44": "partner.supplier",
}


def _versioned_rule(cache_key: str) -> tuple[str, str] | None:
    rule_key = RULE_KEY_BY_CACHE.get(cache_key)
    if not rule_key:
        return None
    try:
        row = query_one(
            """SELECT account_number, account_label FROM account_rules
               WHERE firm_id IS NULL AND rule_key=? AND is_active=1
               ORDER BY effective_from DESC LIMIT 1""", (rule_key,))
        if row:
            return row["account_number"], row["account_label"]
    except Exception:
        # Unit use before DB initialization still falls back to the embedded
        # safe bootstrap values below. Production DB initialization seeds the registry.
        pass
    return None


PURCHASE_NATURE_QUERIES: dict[str, tuple[str, str | None]] = {
    "merchandise": ("achats de marchandises", "61"),
    "raw_material": ("achats matieres premieres", "61"),
    "consumable_supplies": ("achats fournitures consommables", "61"),
    "nonstocked_supplies": ("achats non stockes", "61"),
    "rent": ("locations charges locatives", "61"),
    "maintenance": ("entretien reparations", "61"),
    "insurance": ("primes assurances", "61"),
    "professional_fees": ("honoraires intermediaires", "61"),
    "studies_documentation": ("etudes recherches documentation", "61"),
    "transport": ("transports", "61"),
    "advertising": ("publicite relations publiques", "61"),
    "telecom": ("telecommunications", "61"),
    "banking_services": ("services bancaires", "61"),
    "royalties": ("redevances brevets", "61"),
    "other_external_service": ("imputation a clarifier", "34"),
    "unclassified": ("imputation a clarifier", "34"),
}


# Fixed Moroccan PCG accounts (never resolved dynamically)
CLIENT_ACCOUNT = ("3421", "Clients")            # trade receivables (NOT French 4111)
ESCOMPTE_ACCORDE = ("6386", "Escomptes accordes")
ESCOMPTE_OBTENU = ("7386", "Escomptes obtenus")
DROITS_DE_TIMBRE_ACCOUNT = ("6165", "Droits de timbre")
BANQUE_ACCOUNT = ("5141", "Banque")
CAISSE_ACCOUNT = ("5161", "Caisse")

# Query used per immobilisation type (was duplicated 3x in the monolith)
IMMOBILISATION_QUERIES: dict[str | None, str] = {
    "it": "materiel informatique",
    "furniture": "mobilier de bureau",
    "building": "batiment immeuble",
    "vehicle": "vehicule automobile",
    "installation": "installation equipement",
    "equipment": "materiel outillage",
    "other": "autres immobilisations corporelles",
}
IMMOBILISATION_DEFAULT_QUERY = "autres immobilisations corporelles"


def clear_cache() -> None:
    """Reset the in-process account cache (used by tests)."""
    _account_cache.clear()


async def resolve_account(
    query: str,
    prefix_filter: str | None = None,
    *,
    cache_key: str | None = None,
    min_similarity: float = 0.3,
    timeout: float = 5.0,
    default: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Look up an account via the search service. Returns (code, label).

    Order: cache -> versioned approved registry -> embedded safe bootstrap ->
    optional semantic-search *suggestion* -> explicit default -> ValueError.
    Search output is never statutory authority and is never auto-posted.
    """
    key = cache_key or f"{query}::{prefix_filter}"
    if key in _account_cache:
        return _account_cache[key]

    deterministic = _versioned_rule(key) or ACCOUNT_FALLBACKS.get(key)
    if deterministic is not None:
        _account_cache[key] = deterministic
        logger.info("Account resolved via approved deterministic rule", query=query, code=deterministic[0])
        return deterministic

    if not ACCOUNTS_OFFLINE:
        try:
            payload: dict = {"query": query, "top_k": 1, "min_similarity": min_similarity}
            if prefix_filter:
                payload["prefix_filter"] = prefix_filter
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{ACCOUNT_SEARCH_BASE_URL}/", json=payload)
                resp.raise_for_status()
                data = resp.json()
            if data["matches"]:
                m = data["matches"][0]
                confidence = m.get("confidence", "weak")
                if confidence == "weak":
                    logger.warning(
                        "Account match confidence too low, using fallback",
                        query=query, confidence=confidence, similarity=m.get("similarity"),
                    )
                else:
                    logger.info(
                        "Semantic account suggestion received (not authoritative)",
                        query=query, code=m["account_code"], confidence=confidence,
                    )
        except Exception as e:
            logger.warning(
                "Account search failed, using fallback",
                query=query, prefix_filter=prefix_filter, error=str(e),
            )

    fallback = default
    if fallback is not None:
        _account_cache[key] = fallback
        logger.info("Account resolved via fallback", query=query, code=fallback[0])
        return fallback

    raise ValueError(f"No account found for query: {query!r} and no fallback available")


# --- Named resolvers (thin wrappers preserving the original queries/tuning) ---

async def tva_charge_account() -> tuple[str, str]:
    """TVA recuperable on charges — 34552."""
    return await resolve_account(
        "TVA recuperable charges", "345",
        cache_key="TVA recuperable charges::345",
        min_similarity=0.4, timeout=3,
        default=("34552", "Etat — TVA recuperable (Charges)"),
    )


async def tva_asset_account() -> tuple[str, str]:
    """TVA recuperable on immobilisations — 34551."""
    return await resolve_account(
        "TVA recuperable immobilisations assets", "345",
        cache_key="TVA recuperable immobilisations::345",
        min_similarity=0.4, timeout=3,
        default=("34551", "Etat — TVA recuperable (Immobilisations)"),
    )


async def seller_tva_account() -> tuple[str, str]:
    """Seller output TVA — 4455."""
    return await resolve_account(
        "TVA facturee etat seller output VAT", "44",
        cache_key="TVA facturee::44",
        min_similarity=0.5, timeout=3,
        default=("4455", "Etat TVA facturee"),
    )


async def fournisseur_account() -> tuple[str, str]:
    """Trade payables — 4411."""
    return await resolve_account(
        "fournisseurs suppliers", "44",
        cache_key="fournisseurs::44",
        min_similarity=0.5, timeout=3,
        default=("4411", "Fournisseurs"),
    )


async def ras_account() -> tuple[str, str]:
    """Retenue a la source — 4452."""
    return await resolve_account(
        "retenue a la source withholding tax", "44",
        cache_key="retenue source::44",
        min_similarity=0.5, timeout=3,
        default=("4452", "Etat — Retenue à la source"),
    )


async def immobilisation_account(immobilisation_type: str | None) -> tuple[str, str]:
    """Class 2 (23xx) account for a given asset type."""
    query = IMMOBILISATION_QUERIES.get(immobilisation_type, IMMOBILISATION_DEFAULT_QUERY)
    return await resolve_account(query, prefix_filter="23")


async def purchase_account_for_nature(nature: str | None) -> tuple[str, str]:
    query, prefix = PURCHASE_NATURE_QUERIES.get(nature or "unclassified", PURCHASE_NATURE_QUERIES["unclassified"])
    return await resolve_account(query, prefix_filter=prefix)


async def revenue_purchase_accounts(data: ExtractedInvoiceData) -> tuple[str, str, str, str]:
    """Return the seller revenue and buyer purchase/asset account.

    Generic service invoices are no longer forced into 6121.  The extracted
    accounting nature selects a deterministic PCG family; unknown nature uses
    3497 temporarily and validation blocks posting until a reviewer classifies it.
    """
    is_service = data.invoice_category == "facture_service"
    seller_acct, seller_label = await resolve_account(
        "ventes de services prestations" if is_service else "ventes de marchandises",
        prefix_filter="71",
    )
    if data.is_immobilisation:
        buyer_acct, buyer_label = await immobilisation_account(data.immobilisation_type)
    else:
        default_nature = data.accounting_nature
        if default_nature == "unclassified" and data.invoice_category == "facture_achat":
            default_nature = "merchandise"
        buyer_acct, buyer_label = await purchase_account_for_nature(default_nature)
    return seller_acct, seller_label, buyer_acct, buyer_label
