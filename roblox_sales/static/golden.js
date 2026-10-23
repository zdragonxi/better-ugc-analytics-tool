"use strict";
// Golden Ratio = (pending Robux ÷ items on sale) ÷ average Robux per sale.
// Pending per listed item, measured in "typical sales": how many average sales' worth of
// not-yet-released Robux each item on sale is carrying. Kept in its own file so the formula
// has one definition, used by the dashboard and by tests/test_golden_js.py.
(function (root) {
  // null whenever an input is unknown — never a made-up 0
  function goldenRatio(pending, itemsOnSale, robuxInRange, salesInRange) {
    if (pending == null || !Number.isFinite(pending)) return null;
    if (!itemsOnSale || !Number.isFinite(itemsOnSale)) return null;          // null, 0, NaN
    if (!salesInRange || !Number.isFinite(robuxInRange)) return null;
    const avgPerSale = robuxInRange / salesInRange;
    if (!avgPerSale) return null;
    return (pending / itemsOnSale) / avgPerSale;
  }
  function formatGolden(g) {
    if (g == null) return "–";
    return g >= 0.1 ? g.toFixed(2) : g.toFixed(4);
  }
  root.goldenRatio = goldenRatio;
  root.formatGolden = formatGolden;
})(typeof window !== "undefined" ? window : globalThis);
