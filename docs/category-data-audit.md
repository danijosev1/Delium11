# Category Data-Table / Keepa-Taxonomy Calibration Audit

Status: **resolver mechanism is production-safe; the DATA is not yet trustworthy.**
Scope: the category-dependent lookup tables consumed by `fees.py`, `demand.py`,
and `risk.py`, and the resolver in `analysis/categories.py` (added in `54b1858`).

This audit answers one question precisely: *does an apparently precise
Delium score risk being built on invented or mismatched category data?* The
answer today is **yes for the numeric values** (they are explicitly
illustrative) and **partly for the keys** (some cannot match real Keepa
taxonomy). The resolver itself does not introduce error — it fails safe.

---

## 0. Method and honesty boundary

- **No live Keepa call was made** (no credentials in this environment). The
  "Expected Keepa segment" column below is **best-knowledge, NOT verified** — it
  must be confirmed against a real Keepa `categoryTree` before any value in these
  tables is trusted. Where I am not confident, the status is `NEEDS VERIFICATION`.
- **No Amazon fee rate, BSR curve, or risk value was invented or changed.** All
  values in the three data files remain flagged ILLUSTRATIVE, exactly as shipped.
  Reconciling *keys* to Keepa taxonomy and replacing *values* with verified
  figures is deliberately left to the operator (with credentials), because doing
  it here would mean guessing.
- What *is* verified here is **resolver behavior** (by `tests/test_categories.py`)
  and **internal consistency** of the tables.

`STATUS` legend:
| Status | Meaning |
|---|---|
| `RESOLVER-OK` | The resolver maps this key correctly **iff** the key equals a real Keepa segment; behavior is test-locked. Numeric value may still be illustrative. |
| `NEEDS VERIFICATION` | Key plausibly matches a Keepa root/segment, but the exact string must be checked against a live `categoryTree`. |
| `LIKELY UNREACHABLE` | By best knowledge of Keepa's US taxonomy, this key never appears as a breadcrumb segment, so the rule/rate silently never fires. Verify, then fix or remove. |
| `MISSING` | A real Keepa department that has **no** entry in this table, so it uses the default. |

---

## 1. Resolver safety (VERIFIED by `tests/test_categories.py`)

`analysis/categories.py` matches a table key against the **segments** of the
breadcrumb (split on `> / › » |`, normalized by casefold + whitespace-collapse),
using **segment equality — never substring**.

| STEP 5 requirement | Result | Test |
|---|---|---|
| Exact category keys still work | ✅ | `test_matches_exact_and_within_path` |
| Breadcrumb segment matching works | ✅ | `test_referral_fee_resolves_category_from_breadcrumb_path`, `..._velocity_curve...`, `..._risk_category_rules...` |
| Substring false positives cannot occur | ✅ | `test_match_is_segment_equality_not_substring` |
| `"Books"` cannot match `"Cookbooks"` | ✅ | `test_match_is_segment_equality_not_substring` |
| `"Baby"` cannot match `"Baby Products"` unless intended | ✅ (it does **not**) | `test_singular_key_does_not_match_pluralized_keepa_root` |
| Ambiguous multi-segment matches are deterministic | ✅ (first key in table order) | `test_ambiguous_multi_department_resolution_is_deterministic` |
| No rule silently fires for an unrelated breadcrumb | ✅ | `test_no_unrelated_breadcrumb_silently_fires_a_risk_rule` |
| Unknown category → default in every engine | ✅ | `test_unknown_category_falls_to_default_in_every_engine` |

**Tie-break rule (documented contract):** when more than one key matches a path,
`resolve_category_key` returns the **first key in the table's iteration order**
(TOML declaration order). Real Amazon breadcrumbs have a single department root,
so this is defensive; tables should avoid keys of *different* rates that could
both appear in one path.

**Minor hardening note (not a bug):** the splitter also treats `/` as a
separator. The canonical stored form always uses `" > "`, so this only matters
if a real Keepa category *name* contained `/` — which would cause a **missed**
match (safe fallback to default), never a false positive. No change made.

**Conclusion: category resolution is production-safe.** It cannot manufacture a
match; its only failure mode is failing to match (→ default), which is the
conservative direction.

---

## 2. Inventory & reconciliation

### 2.1 Referral fees — `fee_tables/us-2026.toml [referral.categories]`
Non-default rate is the only thing that changes profit (default 15% applies otherwise).

| Current key | Rate | Expected Keepa segment (UNVERIFIED) | Status | Consequence if wrong | Recommended action |
|---|---|---|---|---|---|
| Home & Kitchen | 0.15 | `Home & Kitchen` | NEEDS VERIFICATION | none (== default) | verify string |
| Kitchen | 0.15 | (subnode, e.g. `Kitchen & Dining`) | LIKELY UNREACHABLE | none (== default) | remove or fix key |
| Electronics | 0.08 | `Electronics` | NEEDS VERIFICATION | **HIGH** — 15% vs 8% ≈ 2× referral, understates margin/ROI | verify string + rate |
| Sports & Outdoors | 0.15 | `Sports & Outdoors` | NEEDS VERIFICATION | none (== default) | verify |
| Toys & Games | 0.15 | `Toys & Games` | NEEDS VERIFICATION | none (== default) | verify |
| Health & Household | 0.15 | `Health & Household` | NEEDS VERIFICATION | none (== default) | verify |
| Beauty & Personal Care | 0.15 | `Beauty & Personal Care` | NEEDS VERIFICATION | none (== default) | verify |
| Pet Supplies | 0.15 | `Pet Supplies` | NEEDS VERIFICATION | none (== default) | verify |
| Office Products | 0.15 | `Office Products` | NEEDS VERIFICATION | none (== default) | verify |
| Grocery & Gourmet Food | 0.08 | `Grocery & Gourmet Food` | NEEDS VERIFICATION | **HIGH** — 15% vs 8% understates margin | verify string + rate |
| Baby | 0.15 | likely `Baby Products` | LIKELY UNREACHABLE | none for referral (== default), but see compliance | fix key → verified Keepa root |
| Tools & Home Improvement | 0.15 | `Tools & Home Improvement` | NEEDS VERIFICATION | none (== default) | verify |

> **Every rate is illustrative.** Even a matching key returns an unverified
> percentage. Amazon's real card has many category-specific rates (e.g. 8%
> electronics, tiered thresholds, 3-tier apparel) that this table does not model.
> A verified rate card must replace these values before profit is trusted.

### 2.2 Closing fees — `[closing.categories]` (media per-item fee)

| Current key | Fee ¢ | Expected Keepa segment (UNVERIFIED) | Status | Consequence if wrong | Recommended action |
|---|---|---|---|---|---|
| Books | 180 | `Books` | NEEDS VERIFICATION (reachable) | media closing miscount | verify fee |
| Music | 180 | Keepa root likely `CDs & Vinyl` | LIKELY UNREACHABLE | closing fee never applied to music | fix key → `CDs & Vinyl` |
| DVD | 180 | Keepa root likely `Movies & TV` | LIKELY UNREACHABLE | never applied | fix key → `Movies & TV` |
| Video, DVD & Blu-ray | 180 | Keepa root likely `Movies & TV` | LIKELY UNREACHABLE | never applied | fix key → `Movies & TV` |

Media categories are outside Delium's price band (18–60 USD private-label focus),
so this table is low-impact, but three of four keys are likely dead.

### 2.3 BSR→velocity curves — `curves_data/bsr_velocity/us.toml`

| Current key | Expected Keepa segment (UNVERIFIED) | Status | Consequence if wrong | Recommended action |
|---|---|---|---|---|
| Home & Kitchen | `Home & Kitchen` | NEEDS VERIFICATION | wrong curve → biased unit estimate; `category_known=False` caps demand confidence at MEDIUM | verify + **calibrate anchors** |
| Sports & Outdoors | `Sports & Outdoors` | NEEDS VERIFICATION | same | verify + calibrate |
| Toys & Games | `Toys & Games` | NEEDS VERIFICATION | same | verify + calibrate |
| *(everything else)* | — | MISSING | uses `[default]` curve, `category_known=False` (confidence capped MEDIUM) | add + calibrate the departments you validate most |

Only **3** departments have curves; every other real product uses the default
curve and is confidence-capped. All anchor values are illustrative — must be
calibrated against observed sales (`delium calibrate`).

### 2.4 IP-risk categories — `risk_data/us.toml [ip].design_patent_categories` (−40)

| Current key | Expected Keepa segment (UNVERIFIED) | Status | Consequence if wrong | Recommended action |
|---|---|---|---|---|
| Toys & Games | `Toys & Games` | NEEDS VERIFICATION | IP risk missed/mis-applied | verify |
| Jewelry | Keepa root likely `Clothing, Shoes & Jewelry` (Jewelry is a subnode) | NEEDS VERIFICATION | may only match as a sub-segment | verify segment presence |
| Clothing, Shoes & Jewelry | `Clothing, Shoes & Jewelry` | NEEDS VERIFICATION | — | verify |
| Cell Phones & Accessories | `Cell Phones & Accessories` | NEEDS VERIFICATION | — | verify |
| Arts, Crafts & Sewing | `Arts, Crafts & Sewing` | NEEDS VERIFICATION | — | verify |

(`brand_likeness_lexicon` matches **titles** by substring, not category — out of
scope for this audit; it is unaffected by the breadcrumb question.)

### 2.5 Compliance categories — `[compliance.categories]` (−30)

| Current key | Expected Keepa segment (UNVERIFIED) | Status | Consequence if wrong | Recommended action |
|---|---|---|---|---|
| Baby | likely `Baby Products` | **LIKELY UNREACHABLE** | **CPSIA deduction MISSED for baby products** — optimistic risk | fix key → verified Keepa root |
| Toys & Games | `Toys & Games` | NEEDS VERIFICATION | CPSIA missed if wrong | verify |
| Health & Household | `Health & Household` | NEEDS VERIFICATION | FDA-labeling flag missed | verify |
| Beauty & Personal Care | `Beauty & Personal Care` | NEEDS VERIFICATION | cosmetics flag missed | verify |
| Grocery & Gourmet Food | `Grocery & Gourmet Food` | NEEDS VERIFICATION | food-contact flag missed | verify |
| Electronics | `Electronics` | NEEDS VERIFICATION | UL/FCC flag missed | verify |
| Tools & Home Improvement | `Tools & Home Improvement` | NEEDS VERIFICATION | UL flag missed | verify |

The **Baby → Baby Products** gap is the single most consequential row: a
children's product that should carry a CPSIA compliance deduction silently
carries none. Highest-priority reconciliation.

### 2.6 High-return categories — `[high_returns].categories` (−20)

| Current key | Expected Keepa segment (UNVERIFIED) | Status | Consequence if wrong | Recommended action |
|---|---|---|---|---|
| Clothing, Shoes & Jewelry | `Clothing, Shoes & Jewelry` | NEEDS VERIFICATION | high-return risk missed | verify |
| Shoes | subnode of `Clothing, Shoes & Jewelry` | LIKELY UNREACHABLE (as root) | may only match as sub-segment | verify |
| Apparel | Keepa uses `Clothing, Shoes & Jewelry` | LIKELY UNREACHABLE | never fires | fix/remove |
| Watches | subnode | LIKELY UNREACHABLE (as root) | may only match as sub-segment | verify |

### 2.7 Not category-keyed (confirmed out of scope, no breadcrumb dependency)
- `[fragility].materials` — matched against **materials**, substring (unchanged).
- `[logistics].oversized_size_tiers` — matched against the fee engine's computed
  **size-tier name** (`large_bulky` etc.), not a category. Consistent vocabulary.

### 2.8 MISSING real departments (no curve/rule at all)
By best knowledge these common Keepa US roots have **no** curve and/or no risk
rule and therefore silently score with defaults (needs verification):
`Automotive`, `Industrial & Scientific`, `Patio, Lawn & Garden`, `Musical
Instruments`, `Appliances`, `Cell Phones & Accessories` (no curve),
`Arts, Crafts & Sewing` (no curve), `Garden & Outdoor`, `Video Games`.
Add curves/rules for the departments you actually validate.

---

## 3. Keepa ingestion audit (STEP 9)

- **Is the stored `category` always a breadcrumb?** Yes. `products.category_path`
  is the only category column; `keepa.py::_extract_category_path` joins
  `categoryTree` node **names** with `" > "` (or `NULL` if `categoryTree` is
  absent). It is never a bare department.
- **Is another canonical department field available?** Yes, at ingestion time but
  **not persisted**: `categoryTree[0].name` is the root department, and Keepa also
  returns `rootCategory` (a numeric category id). The code currently reads neither
  separately.
- **Would storing a normalized root improve correctness?** Yes. An exact match on
  a persisted root department name would eliminate reliance on segment scanning
  and remove the multi-segment tie-break entirely. It would not, however, fix the
  key-string mismatches (a stored `"Baby Products"` root still won't match a
  `"Baby"` table key) — reconciling the table keys is required regardless.
- **Schema change now?** **No — documented as an optional follow-up, not a
  blocker.** The resolver already handles breadcrumbs safely. If pursued later:
  add a nullable `category_root TEXT` column (new migration, never edit an old
  one) populated from `categoryTree[0].name`, and prefer it in `resolve_*`. Not
  implemented in this change.

---

## 4. What must be externally verified (operator checklist, needs Keepa creds)

1. Pull `categoryTree` for ~1 known ASIN per department and record the **exact
   root segment strings** Keepa returns for this account/marketplace.
2. Reconcile every table key above to those strings; fix `Baby`→(root),
   `Music`/`DVD`→(roots), `Shoes`/`Apparel`/`Watches`→(root or subnode), drop
   redundant `Kitchen`.
3. Replace every **illustrative value** (referral %, closing ¢, storage/prep ¢,
   size-tier bands, BSR anchors, risk deductions) with figures verified against
   Amazon's current rate card / Revenue Calculator and observed sales.
4. Add curves and risk rules for the MISSING departments you validate.
5. Re-run `tests/test_categories.py`; update the "Expected Keepa segment" column
   here to VERIFIED once confirmed.

Until steps 1–3 are done, **a Delium profit/risk number is precise but not
accurate** — it is arithmetic over illustrative inputs.

---

## 5. Findings summary

- **New code bug:** none. The resolver is correct and conservative.
- **Data findings:** several keys are likely unreachable (`Baby`, `Music`, `DVD`,
  `Shoes`/`Apparel`, `Kitchen`) and many departments/curves are missing; all
  numeric values are illustrative. None were changed (per the no-invention rule).
- **Highest-priority reconciliation:** `Baby` → Keepa root for the **compliance**
  table (a missed CPSIA deduction is a safety-relevant, verdict-affecting gap).
- **Schema:** optional `category_root` follow-up documented; not a blocker.
