// ==UserScript==
// @name         Binance 双击订单簿一键平多（定制版）
// @namespace    tm.binance.close.long
// @version      1.0.0
// @description  双击订单簿价格 -> 填数量 -> 自动点“平多”
// @match        https://www.binance.com/*/futures/*
// @match        https://www.binance.com/futures/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  const CFG = {
    // 默认平仓数量
    DEFAULT_QTY: '0.002',
    // 防误触：需按住 Shift 再双击
    REQUIRE_SHIFT: true,
    // true=只填数量；false=填数量并自动点“平多”
    SAFE_MODE: false,
    // 防连点
    COOLDOWN_MS: 600,
    DEBUG: true,
  };

  let lastTs = 0;

  const log = (...args) => CFG.DEBUG && console.log('[TM-QuickCloseLong]', ...args);

  function setInputValueReact(input, value) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      'value'
    )?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));
  }

  function findQtyInput() {
    return (
      document.querySelector('input[id^="unitAmount-"]') ||
      document.querySelector('input[aria-label="数量"]') ||
      document.querySelector('input[placeholder="数量"]')
    );
  }

  function findCloseLongButton() {
    const btns = Array.from(document.querySelectorAll('button'));
    return (
      btns.find((b) => {
        const t = (b.textContent || '').trim();
        return t.includes('平多') || t.toLowerCase().includes('close long');
      }) || null
    );
  }

  function isOrderbookPriceNode(node) {
    if (!node) return null;
    return node.closest(
      '#futuresOrderbook .ask-light.emit-price, #futuresOrderbook .bid-light.emit-price'
    );
  }

  function parsePrice(node) {
    const txt = (node.textContent || '').replace(/,/g, '').trim();
    return /^\d+(\.\d+)?$/.test(txt) ? txt : null;
  }

  document.addEventListener('dblclick', (e) => {
    const now = Date.now();
    if (now - lastTs < CFG.COOLDOWN_MS) return;
    if (CFG.REQUIRE_SHIFT && !e.shiftKey) return;

    const priceNode = isOrderbookPriceNode(e.target);
    if (!priceNode) return;

    const clickedPrice = parsePrice(priceNode);
    if (!clickedPrice) return;

    const qtyInput = findQtyInput();
    if (!qtyInput) return log('未找到数量输入框');

    setInputValueReact(qtyInput, CFG.DEFAULT_QTY);
    log('已填数量', CFG.DEFAULT_QTY, '触发价格', clickedPrice);

    if (CFG.SAFE_MODE) {
      lastTs = now;
      return log('SAFE_MODE=true，仅填数量，不点击平多');
    }

    const closeLongBtn = findCloseLongButton();
    if (!closeLongBtn) return log('未找到“平多”按钮');

    closeLongBtn.click();
    lastTs = now;
    log('已点击平多');
  });

  log('脚本加载完成');
})();
