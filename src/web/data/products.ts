import type { Complaint, Competitor, Keyword, Product, RiskFlag } from "../types";

/* ------------------------------------------------------------------ *
 * Deterministic generators so the demo dataset is stable across reloads.
 * ------------------------------------------------------------------ */

function lcg(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/** Weekly series across a 90-day window, with light deterministic jitter. */
function series(start: number, end: number, n = 13, jitter = 0.05, seed = 1): number[] {
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1);
    const base = start + (end - start) * t;
    const noise = Math.sin((i + seed) * 2.3) * base * jitter;
    out.push(Math.max(1, Math.round(base + noise)));
  }
  return out;
}

const BRAND_POOL = [
  "Nuvolo",
  "GreenLeaf",
  "HomeCraft",
  "AmazonBasics",
  "PureNest",
  "EcoBay",
  "NorthPeak",
  "DailyGoods",
  "BrightCo",
  "SimpleWay",
];

function competitorsFor(
  seed: number,
  nouns: string[],
  price: number,
  reviewScale: number
): Competitor[] {
  const r = lcg(seed);
  const out: Competitor[] = [];
  for (let i = 0; i < 10; i++) {
    const brand = BRAND_POOL[(i + seed) % BRAND_POOL.length];
    const noun = nouns[i % nouns.length];
    out.push({
      asin: `B0${(1000 + seed * 7 + i).toString().slice(-6)}`,
      title: `${brand} ${noun}${i % 3 === 1 ? " — 2 Pack" : i % 4 === 2 ? " (Upgraded)" : ""}`,
      brand,
      price: Math.round(price * (0.78 + r() * 0.55) * 100) / 100,
      reviews: Math.max(4, Math.round(reviewScale * (0.3 + r() * 1.7))),
      rating: Math.round((3.7 + r() * 1.2) * 10) / 10,
      ageDays: Math.round(160 + r() * 1700),
      imagesCount: 2 + Math.floor(r() * 7),
    });
  }
  return out;
}

const keywords = (rows: [string, number][]): Keyword[] =>
  rows.map(([phrase, volume]) => ({ phrase, volume }));

const complaints = (rows: [string, number, 1 | 2 | 3, string][]): Complaint[] =>
  rows.map(([theme, freqPct, severity, quote]) => ({ theme, freqPct, severity, quotes: [quote] }));

const risks = (rows: [string, number, string][]): RiskFlag[] =>
  rows.map(([flag, deduction, evidence]) => ({ flag, deduction, evidence }));

/* ------------------------------------------------------------------ *
 * The demo dataset. Eight products spanning the categories and the
 * risk/compliance surfaces the deterministic engine cares about.
 * ------------------------------------------------------------------ */

export const PRODUCTS: Product[] = [
  {
    asin: "B0DLM001",
    marketplace: "US",
    title: "Silicone Baby Food Freezer Tray with Clip-On Lid, 12 x 1oz Portions",
    brand: "Nuvolo",
    category: "Baby Products > Feeding > Food Storage",
    emoji: "🍼",
    hue: 12,
    price: 22.99,
    monthlySold: 200,
    salesSource: "amazon",
    bsr: 1850,
    reviews: 412,
    rating: 4.4,
    reviewsPerMonth: 26,
    ageDays: 620,
    weightG: 320,
    dimsMm: [220, 180, 40],
    fbaFeeCents: 398,
    confidence: "medium",
    why: "Steady 200+/mo, only two page-one listings under 150 reviews, and the lid-cracking complaint recurs across every incumbent.",
    emergence: 42,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(2100, 1640, 13, 0.05, 1),
    soldHistory: series(150, 205, 13, 0.08, 2),
    competitors: competitorsFor(11, [
      "Baby Food Storage Tray",
      "Silicone Freezer Tray",
      "Baby Food Cube Tray",
      "Puree Freezer Tray",
    ], 22.99, 520),
    complaints: complaints([
      ["Lid cracks or splits within weeks", 22, 3, "The lid cracked after two weeks of freezing. Now everything tastes like freezer burn."],
      ["Portions are smaller than expected", 14, 2, "Each cube is way smaller than the photo suggests, a full serving doesn't fit."],
      ["Hard to pop the frozen cubes out", 11, 2, "You have to twist the whole tray and it sprays frozen bits everywhere."],
      ["Lid does not seal, leaks in the bag", 8, 2, "It leaks in the diaper bag even with the lid clipped on."],
      ["Instructions are unclear about dishwasher safety", 6, 1, "No idea if this is dishwasher safe, the insert contradicts the listing."],
    ]),
    missingFeatures: [
      "A lid that survives the freezer (rigid, not soft silicone)",
      "Cubic markings so portions are obvious when frozen",
      "A stackable footprint for fridge door bins",
    ],
    risks: risks([
      ["compliance", 30, "Baby Products → CPSIA / children's product testing"],
      ["seasonality_unknown", 10, "82 days of BSR history — peak concentration unassessable"],
    ]),
    keywords: keywords([
      ["baby food freezer tray", 12400],
      ["silicone baby food storage", 8100],
      ["baby food freezer tray silicone", 5400],
      ["breast milk storage tray", 3300],
      ["baby food storage containers", 2900],
      ["puree freezer tray", 1200],
    ]),
    reviewSample: 208,
  },

  {
    asin: "B0DLM002",
    marketplace: "US",
    title: "Collapsible Travel Dog Bowl, 2-Cup Leak-Proof Silicone with Carabiner",
    brand: "PureNest",
    category: "Pet Supplies > Dogs > Feeding & Watering",
    emoji: "🐕",
    hue: 145,
    price: 19.99,
    monthlySold: 400,
    salesSource: "amazon",
    bsr: 940,
    reviews: 806,
    rating: 4.5,
    reviewsPerMonth: 41,
    ageDays: 880,
    weightG: 210,
    dimsMm: [180, 180, 60],
    fbaFeeCents: 379,
    confidence: "high",
    why: "400+/mo with a healthy 4.5★ — but 19% of reviews complain the rim collapses when full, and no incumbent has fixed it.",
    emergence: 28,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(1250, 880, 13, 0.04, 3),
    soldHistory: series(310, 405, 13, 0.06, 4),
    competitors: competitorsFor(22, [
      "Collapsible Dog Bowl",
      "Travel Dog Water Bowl",
      "Portable Pet Bowl",
      "Foldable Dog Bowl",
    ], 19.99, 900),
    complaints: complaints([
      ["Bowl collapses when full of water", 19, 3, "Filled it at a rest stop and the whole thing folded in on itself, soaked the car."],
      ["Carabiner snaps off", 9, 2, "The clip broke the first time I hung it off my backpack."],
      ["Smells like rubber for weeks", 7, 2, "Even after five washes it still smells strongly of rubber and my dog refuses it."],
      ["Too shallow for a lab-sized dog", 6, 1, "My lab empties it in one lap and then looks at me."],
    ]),
    missingFeatures: [
      "A rigid rim that holds shape when full",
      "A metal carabiner instead of moulded plastic",
      "A raised lip to stop slop in a moving car",
    ],
    risks: risks([["market_concentration", 15, "Brand HHI 0.34 — one brand holds 4 of the top 10 slots"]]),
    keywords: keywords([
      ["collapsible dog bowl", 14800],
      ["travel dog bowl", 6600],
      ["dog water bottle", 5100],
      ["portable dog bowl", 3900],
      ["dog bowl for hiking", 1600],
    ]),
    reviewSample: 312,
  },

  {
    asin: "B0DLM003",
    marketplace: "US",
    title: "Resistance Bands Set with Handles, 11-Piece Stackable up to 150lb",
    brand: "NorthPeak",
    category: "Sports & Outdoors > Exercise & Fitness > Strength Training",
    emoji: "🏋️",
    hue: 205,
    price: 27.5,
    monthlySold: 200,
    salesSource: "estimated",
    bsr: 3400,
    reviews: 1620,
    rating: 4.3,
    reviewsPerMonth: 55,
    ageDays: 1400,
    weightG: 1250,
    dimsMm: [300, 220, 110],
    fbaFeeCents: 539,
    confidence: "medium",
    why: "Deep review moat and three incumbents over 1,000 reviews — the band-snapping complaint is real but the wall is high.",
    emergence: 14,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(2900, 3600, 13, 0.06, 5),
    soldHistory: series(240, 198, 13, 0.08, 6),
    competitors: competitorsFor(33, [
      "Resistance Band Set",
      "Exercise Bands with Handles",
      "Tube Resistance Bands",
      "Workout Band Kit",
    ], 27.5, 1500),
    complaints: complaints([
      ["Bands snap at the carabiner", 21, 3, "The 50lb band snapped mid-row and whipped my shoulder. That is dangerous."],
      ["Handle stitching pulls apart", 12, 2, "After a month the handle seams started coming apart under load."],
      ["Door anchor slips on painted doors", 8, 2, "The anchor slides down the door the moment you pull hard."],
    ]),
    missingFeatures: [
      "Sleeved bands that resist snapping at the clip",
      "A door anchor with a non-slip backing",
    ],
    risks: risks([
      ["keyword_concentration", 15, "68% of cluster volume sits in one phrase"],
      ["supplier_complexity", 10, "Multi-part assembly with rated load claims"],
    ]),
    keywords: keywords([
      ["resistance bands", 40200],
      ["resistance band set with handles", 7100],
      ["exercise bands", 4900],
      ["workout bands", 3200],
    ]),
    reviewSample: 141,
  },

  {
    asin: "B0DLM004",
    marketplace: "US",
    title: "Wooden Montessori Shape Sorter, 12 Blocks with Sensory Textures",
    brand: "BrightCo",
    category: "Toys & Games > Baby & Toddler Toys > Sorting & Stacking",
    emoji: "🧩",
    hue: 35,
    price: 31.0,
    monthlySold: 200,
    salesSource: "amazon",
    bsr: 2600,
    reviews: 288,
    rating: 4.6,
    reviewsPerMonth: 34,
    ageDays: 410,
    weightG: 780,
    dimsMm: [240, 240, 120],
    fbaFeeCents: 497,
    confidence: "medium",
    why: "Young page with a median 288 reviews and strong review velocity — but the toy sits squarely in a design-patent-heavy category.",
    emergence: 63,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(3600, 2400, 13, 0.07, 7),
    soldHistory: series(140, 208, 13, 0.09, 8),
    competitors: competitorsFor(44, [
      "Montessori Shape Sorter",
      "Wooden Sorting Toy",
      "Toddler Shape Puzzle",
      "Sensory Wooden Blocks",
    ], 31.0, 260),
    complaints: complaints([
      ["Paint chips at the edges", 18, 3, "Two blocks lost their paint in the first week and my son puts everything in his mouth."],
      ["Blocks are a choking size for under-3s", 10, 3, "The smaller blocks are small enough to be a real hazard for a 2-year-old."],
      ["Lid does not stay on the box", 9, 2, "The lid just rests on top, so all the blocks end up on the floor."],
      ["Edges are rough, not sanded", 7, 2, "You can feel splinters on the corner of the base board."],
    ]),
    missingFeatures: [
      "Water-based, non-toxic paint that survives teething",
      "A lid that actually latches",
      "Fully rounded, sanded edges",
    ],
    risks: risks([
      ["ip_signal", 40, "Toys & Games is on the design-patent watchlist"],
      ["compliance", 30, "Toys & Games → CPSIA / children's product testing"],
    ]),
    keywords: keywords([
      ["montessori toys for toddlers", 21800],
      ["wooden shape sorter", 7800],
      ["wooden sorting toy", 4300],
      ["toddler learning toys", 3100],
      ["sensory toys for 1 year old", 2400],
    ]),
    reviewSample: 186,
  },

  {
    asin: "B0DLM005",
    marketplace: "US",
    title: "Stainless Steel Bento Lunch Box, 3 Compartments with Leak-Proof Seal",
    brand: "HomeCraft",
    category: "Home & Kitchen > Kitchen & Dining > Storage & Organization",
    emoji: "🍱",
    hue: 190,
    price: 34.99,
    monthlySold: 400,
    salesSource: "amazon",
    bsr: 1200,
    reviews: 530,
    rating: 4.4,
    reviewsPerMonth: 38,
    ageDays: 700,
    weightG: 640,
    dimsMm: [210, 165, 70],
    fbaFeeCents: 497,
    confidence: "high",
    why: "400+/mo, a median 530-review moat, and a compartment seal that leaks in 17% of reviews — a clean, fixable wedge.",
    emergence: 21,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(1500, 1140, 13, 0.05, 9),
    soldHistory: series(320, 410, 13, 0.06, 10),
    competitors: competitorsFor(55, [
      "Stainless Bento Box",
      "Metal Lunch Box",
      "Leak Proof Bento",
      "Adult Bento Lunch Box",
    ], 34.99, 560),
    complaints: complaints([
      ["Compartments leak into each other", 17, 3, "My dressing ended up all over the rice by lunchtime. The seal between compartments is useless."],
      ["Latches feel flimsy", 11, 2, "One latch already bent out of shape after a month of daily use."],
      ["Too heavy to carry daily", 8, 1, "It's beautiful but it weighs a ton once it's full."],
    ]),
    missingFeatures: [
      "Independent gaskets per compartment",
      "Replaceable, thicker latches",
    ],
    risks: risks([["seasonality_confirmed", 20, "Q4 peak concentration 46% of annual demand"]]),
    keywords: keywords([
      ["bento lunch box", 18200],
      ["stainless steel lunch box", 9200],
      ["leak proof lunch box", 4600],
      ["adult bento box", 2800],
    ]),
    reviewSample: 264,
  },

  {
    asin: "B0DLM006",
    marketplace: "US",
    title: "Under-Desk Cable Management Tray, No-Drill Clamp Mount, 24in",
    brand: "SimpleWay",
    category: "Office Products > Office & School Supplies > Desk Accessories",
    emoji: "🖥️",
    hue: 265,
    price: 24.0,
    monthlySold: 200,
    salesSource: "amazon",
    bsr: 4200,
    reviews: 196,
    rating: 4.2,
    reviewsPerMonth: 18,
    ageDays: 380,
    weightG: 980,
    dimsMm: [620, 130, 90],
    fbaFeeCents: 539,
    confidence: "medium",
    why: "Cheap page-one moat (median 196 reviews) but the clamping complaint recurs and the addressable fix is a $0.40 part.",
    emergence: 51,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(5200, 3900, 13, 0.06, 11),
    soldHistory: series(150, 206, 13, 0.08, 12),
    competitors: competitorsFor(66, [
      "Cable Management Tray",
      "Under Desk Cable Tray",
      "Desk Cord Organiser",
      "No Drill Cable Tray",
    ], 24.0, 200),
    complaints: complaints([
      ["Clamp slips on thick desk tops", 24, 3, "My desk is 1.5in thick and the clamp just slides off the moment you put anything in the tray."],
      ["Tray sags in the middle", 13, 2, "Loaded with a power brick it bows and everything slides to the centre."],
      ["Instructions are a single confusing diagram", 9, 1, "Took me twenty minutes to work out which way the bracket faces."],
    ]),
    missingFeatures: [
      "A clamp that grips 2in+ desk tops",
      "A centre support rib to stop sag",
    ],
    risks: risks([["keyword_concentration", 15, "72% of cluster volume in one phrase"]]),
    keywords: keywords([
      ["under desk cable management", 9800],
      ["cable management tray", 5400],
      ["desk cable organizer", 4100],
      ["cord management under desk", 1300],
    ]),
    reviewSample: 132,
  },

  {
    asin: "B0DLM007",
    marketplace: "US",
    title: "Glass Meal Prep Containers, 10-Piece Borosilicate Set with Bamboo Lids",
    brand: "PureNest",
    category: "Home & Kitchen > Kitchen & Dining > Cookware",
    emoji: "🥡",
    hue: 160,
    price: 42.0,
    monthlySold: 100,
    salesSource: "estimated",
    bsr: 6200,
    reviews: 2140,
    rating: 4.1,
    reviewsPerMonth: 30,
    ageDays: 1900,
    weightG: 4200,
    dimsMm: [220, 160, 320],
    fbaFeeCents: 640,
    confidence: "medium",
    why: "Deep moat, glass fragility, and the config `avoid` list names glass — this is the AVOID teaching example.",
    emergence: 9,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(5400, 6500, 13, 0.07, 13),
    soldHistory: series(140, 96, 13, 0.1, 14),
    competitors: competitorsFor(77, [
      "Glass Meal Prep Containers",
      "Borosilicate Food Containers",
      "Glass Lunch Box Set",
      "Glass Food Storage Set",
    ], 42.0, 2100),
    complaints: complaints([
      ["Lids crack in the freezer", 26, 3, "Three lids shattered the first time I froze soup. Bamboo does not like the cold."],
      ["Containers chip at the rim", 19, 2, "Every single one has a chip on the rim after two months."],
      ["Too heavy to take to work", 14, 2, "The set weighs more than my laptop bag."],
      ["Bamboo lids absorb smells", 11, 2, "The lids now permanently smell of curry no matter how I wash them."],
    ]),
    missingFeatures: ["Freezer-safe lids", "A lighter, stackable profile"],
    risks: risks([
      ["fragility", 20, "Glass / ceramic material lexicon hit"],
      ["seasonality_confirmed", 20, "Peak concentration 44%"],
      ["market_concentration", 15, "Brand HHI 0.31"],
    ]),
    keywords: keywords([
      ["glass meal prep containers", 14200],
      ["glass food storage", 8100],
      ["borosilicate containers", 2200],
    ]),
    reviewSample: 118,
  },

  {
    asin: "B0DLM008",
    marketplace: "US",
    title: "Weighted Sleep Sack 1.0 TOG, 2-Way Zip, 12-18 Months",
    brand: "Nuvolo",
    category: "Baby Products > Nursery > Bedding",
    emoji: "🌙",
    hue: 230,
    price: 28.5,
    monthlySold: 200,
    salesSource: "amazon",
    bsr: 2100,
    reviews: 356,
    rating: 4.5,
    reviewsPerMonth: 29,
    ageDays: 520,
    weightG: 480,
    dimsMm: [280, 200, 50],
    fbaFeeCents: 445,
    confidence: "high",
    why: "Strong star rating and steady volume, but the zip-blocking fabric complaint shows up in a fifth of reviews on every listing.",
    emergence: 34,
    pillars: { demand: null, competition: null, differentiation: null, profitability: null, risk: null },
    bsrHistory: series(2450, 2020, 13, 0.05, 15),
    soldHistory: series(160, 208, 13, 0.07, 16),
    competitors: competitorsFor(88, [
      "Weighted Sleep Sack",
      "Baby Sleep Bag",
      "Toddler Wearable Blanket",
      "TOG Sleep Sack",
    ], 28.5, 400),
    complaints: complaints([
      ["Fabric catches in the zip", 21, 3, "The zip snags the inner lining constantly and I'm terrified it will catch the baby's leg."],
      ["Weight is unevenly distributed", 12, 2, "All the beads settle to the bottom after a wash, so the shoulders have no weight at all."],
      ["Shrinks badly on a warm wash", 10, 2, "Followed the label and it still shrank a full size."],
    ]),
    missingFeatures: [
      "A zip guard strip so the lining cannot catch",
      "Baffled weight pockets that survive washing",
    ],
    risks: risks([
      ["compliance", 30, "Baby Products → CPSIA / children's product testing"],
      ["market_concentration", 15, "Brand HHI 0.33"],
    ]),
    keywords: keywords([
      ["weighted sleep sack", 11600],
      ["baby sleep sack", 7900],
      ["tog sleep sack", 3900],
      ["wearable blanket", 2100],
    ]),
    reviewSample: 241,
  },
];

export const productByAsin = (asin?: string) => PRODUCTS.find((p) => p.asin === asin);
