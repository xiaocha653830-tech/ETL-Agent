/* ==========================================================================
   ETL-Agent 原型 · 共享骨架
   侧栏 / 顶栏集中渲染，页面只写内容区，避免 5 份重复代码。
   ========================================================================== */
(function () {
  'use strict';

  // ---- 导航注册表：新增页面只需在此加一行 --------------------------------
  var PAGES = [
    { id: 'dashboard',  label: '总览工作台',      icon: 'fa-th-large',    href: 'dashboard.html' },
    { id: 'studio',     label: 'Pipeline Studio', icon: 'fa-sitemap',     href: 'studio.html', badge: '2' },
    { id: 'runs',       label: '运行与审批',      icon: 'fa-play-circle', href: 'runs.html',   badge: '3' },
    { id: 'governance', label: '安全治理',        icon: 'fa-shield',      href: 'governance.html' }
  ];

  var PAGE_META = {
    dashboard:  { title: '总览工作台',      crumb: 'WORKSPACE / OVERVIEW' },
    studio:     { title: 'Pipeline Studio', crumb: 'WORKSPACE / STUDIO' },
    runs:       { title: '运行中心与审批',  crumb: 'OPERATIONS / RUNS & APPROVAL' },
    governance: { title: '安全治理与评测',  crumb: 'GOVERNANCE / BENCHMARK' },
    login:      { title: '登录',            crumb: 'AUTH' }
  };

  var current = document.body.dataset.page || 'dashboard';

  // ---- 侧栏 --------------------------------------------------------------
  function renderSidebar() {
    var host = document.getElementById('sidebar');
    if (!host) return;

    var nav = PAGES.map(function (p) {
      var active = p.id === current ? ' is-active' : '';
      var badge = p.badge ? '<em class="nav__badge">' + p.badge + '</em>' : '';
      return '<a class="nav__item' + active + '" href="' + p.href + '"' +
             (active ? ' aria-current="page"' : '') + '>' +
             '<i class="fa ' + p.icon + '"></i><span>' + p.label + '</span>' + badge +
             '</a>';
    }).join('');

    host.innerHTML =
      '<div class="brand">' +
        '<div class="brand__mark"><i class="fa fa-shield"></i></div>' +
        '<div class="brand__text">' +
          '<div class="brand__name">ETL-Agent</div>' +
          '<div class="brand__sub">Control Plane</div>' +
        '</div>' +
      '</div>' +
      '<nav class="nav" aria-label="主导航">' +
        '<div class="nav__label">工作区</div>' + nav +
      '</nav>' +
      '<div class="kernel">' +
        '<div class="kernel__head"><span class="led led--run"></span>安全内核 · 运行中</div>' +
        '<div class="kernel__row"><span>Tool Broker</span><b><span class="led led--ok"></span> 唯一出口</b></div>' +
        '<div class="kernel__row"><span>防重放 Guard</span><b>启用</b></div>' +
        '<div class="kernel__row"><span>证据账本</span><b>链校验通过</b></div>' +
      '</div>' +
      '<div class="userbox">' +
        '<div class="avatar">王</div>' +
        '<div class="userbox__meta">' +
          '<div class="userbox__name">王五</div>' +
          '<div class="userbox__role">数据工程师 · Maker</div>' +
        '</div>' +
      '</div>';
  }

  // ---- 顶栏 --------------------------------------------------------------
  function renderTopbar() {
    var host = document.getElementById('topbar');
    if (!host) return;
    var meta = PAGE_META[current] || PAGE_META.dashboard;

    host.innerHTML =
      '<div class="topbar__title">' +
        '<h1>' + meta.title + '</h1>' +
        '<span class="topbar__crumb">' + meta.crumb + '</span>' +
      '</div>' +
      '<div class="topbar__spacer"></div>' +
      '<button class="projpick"><i class="fa fa-cube"></i>项目: 订单域<i class="fa fa-angle-down"></i></button>' +
      '<button class="btn btn--ghost btn--sm"><i class="fa fa-bell-o"></i>通知' +
        '<span class="nav__badge" style="margin-left:2px">3</span></button>' +
      '<button class="btn btn--sm"><i class="fa fa-question-circle-o"></i>帮助</button>';
  }

  // ---- 轻量交互：Tab 切换 / 分段选择 --------------------------------------
  function bindTabs() {
    document.querySelectorAll('[data-tabs]').forEach(function (group) {
      var tabs   = group.querySelectorAll('.tab');
      var panes  = document.querySelectorAll('[data-pane]');
      tabs.forEach(function (tab) {
        tab.addEventListener('click', function () {
          tabs.forEach(function (t) { t.classList.remove('is-active'); });
          tab.classList.add('is-active');
          var key = tab.dataset.tab;
          panes.forEach(function (p) {
            if (p.dataset.pane === key || p.dataset.pane === '*') {
              p.hidden = false;
            } else {
              p.hidden = true;
            }
          });
        });
      });
      // 初始化：只显示默认 pane
      var first = group.querySelector('.tab.is-active') || tabs[0];
      if (first) {
        var key = first.dataset.tab;
        panes.forEach(function (p) {
          p.hidden = !(p.dataset.pane === key || p.dataset.pane === '*');
        });
      }
    });
  }

  // ---- 轻量交互：确认类按钮的即时反馈 ------------------------------------
  function bindActions() {
    document.querySelectorAll('[data-toast]').forEach(function (el) {
      el.addEventListener('click', function (e) {
        e.preventDefault();
        var bar = document.getElementById('proto-toast');
        if (!bar) {
          bar = document.createElement('div');
          bar.id = 'proto-toast';
          bar.style.cssText =
            'position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(20px);' +
            'background:#101a22;border:1px solid #24384a;border-left:2px solid #2dd4bf;' +
            'color:#e6eff6;padding:10px 18px;border-radius:6px;font-size:12.5px;z-index:9999;' +
            'box-shadow:0 24px 60px -20px rgba(0,0,0,.85);opacity:0;transition:.28s;';
          document.body.appendChild(bar);
        }
        bar.textContent = '原型演示：' + el.dataset.toast;
        requestAnimationFrame(function () {
          bar.style.opacity = '1';
          bar.style.transform = 'translateX(-50%) translateY(0)';
        });
        clearTimeout(bar._t);
        bar._t = setTimeout(function () {
          bar.style.opacity = '0';
          bar.style.transform = 'translateX(-50%) translateY(20px)';
        }, 2200);
      });
    });
  }

  // ---- 进度条 / 计量条按 data 值渲染 -------------------------------------
  function hydrateMeters() {
    document.querySelectorAll('.meter__fill[data-value]').forEach(function (el) {
      var v = Math.max(0, Math.min(100, parseFloat(el.dataset.value) || 0));
      requestAnimationFrame(function () { el.style.width = v + '%'; });
      if (!el.style.width) el.style.width = '0%';
    });
    document.querySelectorAll('.segbar[data-value]').forEach(function (el) {
      var total = el.children.length;
      var v = Math.max(0, Math.min(100, parseFloat(el.dataset.value) || 0));
      var on = Math.round(total * v / 100);
      Array.prototype.forEach.call(el.children, function (seg, i) {
        seg.classList.toggle('on', i < on);
      });
    });
  }

  function init() {
    renderSidebar();
    renderTopbar();
    bindTabs();
    bindActions();
    hydrateMeters();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
