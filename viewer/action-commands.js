(function (root) {
  'use strict';
  const numbers = { one: 1, two: 2, three: 3, four: 4, five: 5, ten: 10, fifteen: 15, twenty: 20, thirty: 30, sixty: 60 };
  const duration = '(\\d+|one|two|three|four|five|ten|fifteen|twenty|thirty|sixty)\\s+(minutes?|hours?|days?)';
  const durationPattern = new RegExp('^' + duration + '$', 'i');

  function afterDuration(text, now) {
    const match = String(text).match(durationPattern);
    if (!match) return null;
    const n = Number(match[1]) || numbers[match[1].toLowerCase()];
    const multiplier = /^minute/i.test(match[2]) ? 60000 : /^hour/i.test(match[2]) ? 3600000 : 86400000;
    if (!n || n > 365) return null;
    const value = new Date(now.getTime() + n * multiplier);
    return Number.isNaN(value.getTime()) ? null : value.toISOString();
  }

  function localInstant(value) {
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$/);
    if (!match) return null;
    const parts = match.slice(1).map(Number);
    const date = new Date(parts[0], parts[1] - 1, parts[2], parts[3], parts[4]);
    if (date.getFullYear() !== parts[0] || date.getMonth() !== parts[1] - 1 ||
        date.getDate() !== parts[2] || date.getHours() !== parts[3] || date.getMinutes() !== parts[4]) return null;
    return date.toISOString();
  }

  function namedDayInstant(day, hourText, minuteText, meridiem, now) {
    let hour = Number(hourText);
    const minute = Number(minuteText || 0);
    if (minute > 59 || hour > (meridiem ? 12 : 23) || (meridiem && hour < 1)) return null;
    if (meridiem) hour = hour % 12 + (meridiem.toLowerCase() === 'pm' ? 12 : 0);
    const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + (day.toLowerCase() === 'tomorrow' ? 1 : 0));
    date.setHours(hour, minute, 0, 0);
    if (date.getHours() !== hour || date.getMinutes() !== minute) return null;
    return date.toISOString();
  }

  function parse(text, now = new Date()) {
    const clean = String(text || '').replace(/^\s*(?:(?:hey|hello)\s+)?jarvis[\s,.:!]*/i, '')
      .replace(/[.!?]+$/, '').replace(/\s+please$/i, '').trim();
    let m;
    const request = clean.replace(/^(?:(?:please|can you|could you)\s+)?/i, '');
    if (/^(?:open|launch|start|visit|go to)(?:\s+(?:a|the|this)\s+(?:website|site|page))?$/i.test(request)) {
      return {type: 'clarify'};
    }
    if (/^(?:open|launch|start)\s+(?:(?:the|my)\s+)?(?:file|windows)\s+explorer$/i.test(request)) {
      return {type: 'open_file_explorer'};
    }
    const browserName = '(?:(?:mozilla\\s+)?firefox|(?:google\\s+)?chrome|(?:microsoft\\s+)?edge|(?:web\\s+)?browser)';
    const spoken = request.replace(/\s+dot\s+/gi, '.');
    const address = '((?:https?:\\/\\/)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+(?:[\\/?#][^\\s]*)?)';
    const site = spoken.match(new RegExp('^(?:open|visit|go to|navigate to)\\s+' + address +
      '(?:\\s+in\\s+(?:a\\s+)?new\\s+tab)?(?:\\s+(?:in|on)\\s+(?:(?:the|my)\\s+)?(' + browserName + '))?$', 'i'));
    if (site) {
      const name = (site[2] || '').toLowerCase();
      const browser = name.includes('firefox') ? 'firefox' : name.includes('chrome') ? 'chrome' : name.includes('edge') ? 'edge' : 'default';
      const url = /^https?:\/\//i.test(site[1]) ? site[1] : 'https://' + site[1];
      return {type: 'open_url', browser, new_tab: true, url};
    }
    const newTab = request.match(new RegExp('^(?:open|create|start)\\s+(?:(?:a|the|another)\\s+)?(?:new\\s+)?tab(?:\\s+(?:in|on)\\s+(?:(?:the|my)\\s+)?(' + browserName + '))?$', 'i'));
    const openBrowser = request.match(new RegExp('^(?:open|launch|start)\\s+(?:(?:a|the|another|my)\\s+)?(?:new\\s+)?(' + browserName + ')(?:\\s+browser)?(?:\\s+in\\s+(?:a\\s+)?new\\s+tab)?$', 'i'));
    if (newTab || openBrowser) {
      const name = ((newTab ? newTab[1] : openBrowser[1]) || '').toLowerCase();
      const browser = name.includes('firefox') ? 'firefox' : name.includes('chrome') ? 'chrome' : name.includes('edge') ? 'edge' : 'default';
      return {type: 'open_browser', browser, new_tab: !!newTab || /\bin\s+(?:a\s+)?new\s+tab$/i.test(request)};
    }
    if (/^(?:show|list|what are)\s+(?:my\s+)?tasks$/i.test(clean)) return {type: 'list', kind: 'task'};
    if (/^(?:show|list|what are)\s+(?:my\s+)?reminders$/i.test(clean)) return {type: 'list', kind: 'reminder'};
    if ((m = clean.match(/^(?:add|create)\s+(?:a\s+)?task(?:\s+to)?\s+(.+)$/i))) return {type: 'create', kind: 'task', title: m[1]};
    if ((m = clean.match(/^(?:complete|finish)\s+(?:the\s+)?task\s+(.+)$/i))) return {type: 'complete', title: m[1]};
    if ((m = clean.match(new RegExp('^postpone\\s+(?:the\\s+)?task\\s+(.+?)\\s+(?:by|for)\\s+(' + duration + ')$', 'i')))) {
      return {type: 'postpone', title: m[1], due_at: afterDuration(m[2], now)};
    }
    if ((m = clean.match(new RegExp('^remind\\s+me\\s+in\\s+(' + duration + ')\\s+to\\s+(.+)$', 'i')))) {
      return {type: 'create', kind: 'reminder', title: m[4], due_at: afterDuration(m[1], now)};
    }
    if ((m = clean.match(new RegExp('^remind\\s+me\\s+to\\s+(.+?)\\s+in\\s+(' + duration + ')$', 'i')))) {
      return {type: 'create', kind: 'reminder', title: m[1], due_at: afterDuration(m[2], now)};
    }
    if ((m = clean.match(/^remind\s+me\s+to\s+(.+?)\s+at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})$/i))) {
      return {type: 'create', kind: 'reminder', title: m[1], due_at: localInstant(m[2])};
    }
    if ((m = clean.match(/^remind\s+me\s+to\s+(.+?)\s+(today|tomorrow)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i))) {
      return {type: 'create', kind: 'reminder', title: m[1], due_at: namedDayInstant(m[2], m[3], m[4], m[5], now)};
    }
    if (/^(?:research|investigate|search\s+(?:the\s+)?web\s+for|find\s+(?:the\s+)?latest\s+information\s+about|what\s+is\s+happening\s+with|look\s+up)\s+\S/i.test(clean)) return {type: 'research', text: clean};
    return {type: 'unknown'};
  }

  const api = {parse, localInstant};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.JarvisActionCommands = api;
})(typeof globalThis !== 'undefined' ? globalThis : window);
