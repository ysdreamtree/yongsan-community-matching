/* ============================================================
   registration-sheet.gs  —  접수를 구글 시트에 쌓고, 사업별 담당자에게 메일을 보내고,
                              관심 숫자를 화면에 실시간으로 보여주는 스크립트

   [설정 방법 · 10분]
   1) 구글 시트를 새로 만들고 [확장 프로그램] → [Apps Script] 를 엽니다.
   2) 기존 코드를 지우고 이 파일 내용을 붙여넣은 뒤 저장합니다.
   3) 위쪽 함수 선택 칸에서 setup 을 골라 [실행] 합니다. (권한 허용 화면이 나옵니다)
      → '접수' 시트와 '사업담당' 시트가 만들어집니다.
   4) '사업담당' 시트의 C열에 사업별 담당자 이메일을 적습니다. (여러 명은 쉼표로)
      비워 둔 사업의 접수는 이 시트를 만든 계정(총괄)으로 메일이 갑니다.
   5) [배포] → [새 배포] → 유형 '웹 앱'
        · 다음 사용자로 실행: 나
        · 액세스 권한: 모든 사용자
      를 고르고 배포한 뒤 '웹 앱 URL'(…/exec)을 복사합니다.
   6) programs.js 의 registerUrl 에 그 URL을, consent 에 보유 기간과 문의처를 적습니다.
      (보유 기간·문의처가 비어 있으면 화면이 접수를 켜지 않습니다.)

   ※ 이 파일을 고친 뒤에는 [배포] → [배포 관리] → 새 버전으로 다시 배포해야 반영됩니다.
   ※ 메일 발송 한도: 일반 구글 계정 하루 약 100통, 구글 워크스페이스 계정 약 1,500통.
   ※ 화면에 보이는 숫자는 사업별 접수 건수뿐입니다. 연락처·담당자 메일은 화면으로 나가지 않습니다.
   ============================================================ */

var SHEET_IN = '접수';
var SHEET_PROG = '사업담당';
var HEAD = ['접수시각', '접수번호', '유형', '관심·관련 사업', '기관·업체명', '구분',
            '담당자 성함', '연락처', '내용', '요약 공개', '개인정보 동의', '처리상태', '메일 발송', '사업ID'];
var TYPES = { interest: '관심 사업', together: '협력 제안', resource: '자원 연계 제안', recommend: '협력 대상 추천' };
var STATUS_LIST = ['접수', '확인 중', '연락 완료', '협의 중', '완료', '제외(스팸)'];
var DEFAULT_PROGRAMS = [
  ['book-support', '도서 지원'],
  ['reading-spread', '기업·기관 독서경영 확산'],
  ['culture-art', '문화예술 협력 프로젝트'],
  ['local-activity', '지역 독서·문화활동']
];

/* 처음 한 번만 실행하세요. */
function setup() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var a = ss.getSheetByName(SHEET_IN) || ss.insertSheet(SHEET_IN);
  if (a.getLastRow() === 0) {
    a.appendRow(HEAD);
    a.setFrozenRows(1);
    a.getRange(1, 1, 1, HEAD.length).setFontWeight('bold');
  }
  a.getRange(2, 12, 2000, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(STATUS_LIST, true).build());
  var p = ss.getSheetByName(SHEET_PROG) || ss.insertSheet(SHEET_PROG);
  if (p.getLastRow() === 0) {
    p.appendRow(['사업ID', '사업명', '담당자 이메일 (여러 명은 쉼표로)']);
    p.getRange(1, 1, 1, 3).setFontWeight('bold');
    DEFAULT_PROGRAMS.forEach(function (r) { p.appendRow([r[0], r[1], '']); });
    p.setFrozenRows(1);
  }
}

/* ---------- 접수 ---------- */
function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(10000);
    var d = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (d.hp) return out({ ok: true, id: '-' });               // 자동 입력 방지: 조용히 무시

    var type = String(d.type || '');
    if (!TYPES[type]) return fail('요청을 확인할 수 없습니다.');
    var rec = type === 'recommend';
    var org = clean(d.org, 80), kind = clean(d.kind, 20), person = clean(d.person, 40),
        contact = clean(d.contact, 100), body = clean(d.body, 1500);

    if (!org) return fail('기관·업체명을 입력해 주세요.');
    if (!rec) {
      if (!person || !contact) return fail('담당자 성함과 연락처를 입력해 주세요.');
      if (d.consent !== true) return fail('개인정보 수집·이용에 동의해 주세요.');
    }
    if (type !== 'interest' && !body) return fail('내용을 입력해 주세요.');

    var known = programMap();
    var ids = [];
    (d.programs || []).forEach(function (id) {
      id = String(id);
      if (known[id] && ids.indexOf(id) < 0) ids.push(id);
    });
    if (type === 'interest' && !ids.length) return fail('관심 사업을 한 개 이상 선택해 주세요.');

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sh = ss.getSheetByName(SHEET_IN);
    if (!sh) return fail('접수 시트가 준비되지 않았습니다.');

    // 짧은 시간에 너무 많이 들어오면 잠시 막습니다.
    var last = sh.getLastRow();
    if (last > 1) {
      var from = Math.max(2, last - 99);
      var times = sh.getRange(from, 1, last - from + 1, 1).getValues();
      var recent = 0, cutoff = Date.now() - 10 * 60 * 1000;
      times.forEach(function (r) { var t = new Date(r[0]).getTime(); if (t > cutoff) recent++; });
      if (recent >= 60) return fail('접수가 많아 잠시 후 다시 시도해 주세요.');
    }

    var now = new Date();
    var id = 'R' + Utilities.formatDate(now, 'Asia/Seoul', 'yyMMdd') + '-' + pad(last, 3);
    var names = ids.map(function (i) { return known[i].name; }).join(', ');
    var row = [now, id, TYPES[type], names, org, rec ? '' : kind, rec ? '' : person, rec ? '' : contact,
               body, d.pub === true ? '동의' : '', rec ? '해당 없음' : '동의', '접수', '', ids.join(',')];
    sh.appendRow(row);
    var rowNo = sh.getLastRow();
    sh.getRange(rowNo, 13).setValue(notify(row, ids, known, ss.getUrl()));

    CacheService.getScriptCache().remove('counts');
    return out({ ok: true, id: id });
  } catch (ex) {
    return out({ ok: false, error: '접수 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.' });
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}

/* ---------- 관심 숫자 ---------- */
function doGet(e) {
  var action = (e && e.parameter && e.parameter.action) || '';
  if (action === 'counts') {
    var cache = CacheService.getScriptCache();
    var hit = cache.get('counts');
    if (hit) return json(hit);
    var s = JSON.stringify(computeCounts());
    cache.put('counts', s, 10);
    return json(s);
  }
  return json(JSON.stringify({ ok: true }));
}

function computeCounts() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_IN);
  var counts = {}, total = 0;
  var n = sh ? sh.getLastRow() - 1 : 0;
  if (n > 0) {
    sh.getRange(2, 12, n, 3).getValues().forEach(function (r) {
      if (String(r[0]).indexOf('제외') === 0) return;            // 스팸으로 표시한 줄은 세지 않음
      total++;
      String(r[2]).split(',').forEach(function (id) {
        id = id.trim();
        if (id) counts[id] = (counts[id] || 0) + 1;
      });
    });
  }
  return { ok: true, counts: counts, total: total, updated: new Date().toISOString() };
}

/* ---------- 사업별 담당자 메일 ---------- */
function notify(row, ids, known, sheetUrl) {
  var owner = Session.getEffectiveUser().getEmail();
  var map = {};                                                 // 메일주소 → 해당 사업명들
  ids.forEach(function (id) {
    var list = splitEmails(known[id].emails);
    if (!list.length) list = [owner];
    list.forEach(function (m) { (map[m] = map[m] || []).push(known[id].name); });
  });
  if (!ids.length) map[owner] = [];
  var result = [];
  Object.keys(map).forEach(function (m) {
    try {
      var lines = [
        '지역상생 협력 플랫폼에 새 접수가 있습니다.', '',
        '접수번호: ' + row[1],
        '유형: ' + row[2],
        '관련 사업: ' + (map[m].length ? map[m].join(', ') : '지정 없음'),
        '기관·업체명: ' + row[4]
      ];
      if (row[5]) lines.push('구분: ' + row[5]);
      if (row[6]) lines.push('담당자: ' + row[6]);
      if (row[7]) lines.push('연락처: ' + row[7]);
      if (row[8]) lines.push('내용: ' + row[8]);
      lines.push('', '접수 현황: ' + sheetUrl);
      MailApp.sendEmail({
        to: m,
        subject: '[지역상생 협력 플랫폼] 새 접수 - ' + row[4],
        body: lines.join('\n'),
        name: '지역상생 협력 플랫폼'
      });
      result.push(m + ' 발송');
    } catch (x) {
      result.push(m + ' 실패');
    }
  });
  return result.join(', ');
}

/* ---------- 도우미 ---------- */
function programMap() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_PROG);
  var map = {};
  if (!sh || sh.getLastRow() < 2) return map;
  sh.getRange(2, 1, sh.getLastRow() - 1, 3).getValues().forEach(function (r) {
    var id = String(r[0]).trim();
    if (id) map[id] = { name: String(r[1] || id), emails: String(r[2] || '') };
  });
  return map;
}
function splitEmails(s) {
  return String(s || '').split(/[,;\s]+/).filter(function (x) { return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(x); });
}
// 글자 수를 제한하고, 시트에서 수식으로 실행되지 않도록 앞의 기호를 막습니다.
function clean(v, max) {
  var s = String(v == null ? '' : v).replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, '').trim().slice(0, max);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}
function pad(n, w) { var s = String(n); while (s.length < w) s = '0' + s; return s; }
function out(o) { return json(JSON.stringify(o)); }
function fail(msg) { return out({ ok: false, error: msg }); }
function json(s) { return ContentService.createTextOutput(s).setMimeType(ContentService.MimeType.JSON); }
