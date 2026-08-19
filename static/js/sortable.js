/* Click-to-sort for the site's static tables.
 *
 * Applies to .standings-table and .stat-table. Tables that render and sort
 * themselves in JS (Players, Draft Lab, the draft board grid) opt out with
 * data-nosort, as does any table whose row order carries meaning on its own.
 *
 * Cell values are read from data-val when present, otherwise parsed from the
 * text: a leading number wins (so "25–12", "+7.3", "3,460.8" and "72.8%" all
 * sort numerically), and anything else falls back to a locale compare.
 */
(function () {
  var NUM = /^[+-]?[\d,]*\.?\d+/;

  function cellValue(row, idx) {
    var cell = row.children[idx];
    if (!cell) return { n: null, s: '' };
    if (cell.dataset && cell.dataset.val !== undefined) {
      var dv = cell.dataset.val;
      var dn = parseFloat(dv);
      return { n: isNaN(dn) ? null : dn, s: dv };
    }
    var text = (cell.textContent || '').trim();
    var m = text.replace(/[,\s]/g, '').match(NUM);
    return { n: m ? parseFloat(m[0]) : null, s: text.toLowerCase() };
  }

  function makeSortable(table) {
    var head = table.tHead;
    var body = table.tBodies[0];
    if (!head || !body || body.rows.length < 2) return;
    var ths = head.rows[head.rows.length - 1].cells;

    Array.prototype.forEach.call(ths, function (th, idx) {
      th.classList.add('is-sortable');
      th.tabIndex = 0;
      th.setAttribute('role', 'button');

      function sort() {
        var asc = th.dataset.dir !== 'asc';
        Array.prototype.forEach.call(ths, function (o) {
          if (o !== th) { o.removeAttribute('data-dir'); o.classList.remove('sort-asc', 'sort-desc'); }
        });
        th.dataset.dir = asc ? 'asc' : 'desc';
        th.classList.toggle('sort-asc', asc);
        th.classList.toggle('sort-desc', !asc);

        var rows = Array.prototype.slice.call(body.rows);
        rows.forEach(function (r, i) { r._i = i; });   // stable tiebreak
        rows.sort(function (a, b) {
          var x = cellValue(a, idx), y = cellValue(b, idx), c;
          if (x.n !== null && y.n !== null) c = x.n - y.n;
          else if (x.n !== null) c = -1;
          else if (y.n !== null) c = 1;
          else c = x.s.localeCompare(y.s);
          if (c === 0) c = a._i - b._i;
          return asc ? c : -c;
        });
        var frag = document.createDocumentFragment();
        rows.forEach(function (r) { frag.appendChild(r); });
        body.appendChild(frag);
      }

      th.addEventListener('click', sort);
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); }
      });
    });
  }

  function init() {
    var tables = document.querySelectorAll('table.standings-table, table.stat-table');
    Array.prototype.forEach.call(tables, function (t) {
      if (t.hasAttribute('data-nosort')) return;
      if (t.classList.contains('sortable')) return;   // has its own handler
      makeSortable(t);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
