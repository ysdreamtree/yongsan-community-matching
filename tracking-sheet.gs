/* ============================================================
   tracking-sheet.gs  —  버튼 클릭을 구글 시트에 한 줄씩 쌓는 스크립트

   [설정 방법 · 5분]
   1) 구글 시트를 새로 만들고, 메뉴 [확장 프로그램] → [Apps Script] 를 엽니다.
   2) 기존 코드를 모두 지우고 이 파일의 내용을 붙여넣은 뒤 저장합니다.
   3) 오른쪽 위 [배포] → [새 배포] → 유형 '웹 앱'
        · 다음 사용자로 실행: 나
        · 액세스 권한: 모든 사용자
      를 고르고 배포한 뒤, 나오는 '웹 앱 URL'을 복사합니다.
   4) programs.js 의 analytics.sheetUrl 에 그 URL을 붙여넣습니다.
   5) 페이지에서 버튼을 눌러 보면 시트에 줄이 생깁니다.

   [기록되는 것] 시각 · 이벤트 · 사업 · 제안 종류 · 페이지
   [기록되지 않는 것] 이름 · 연락처 · 제안 내용 (페이지가 보내지 않습니다)

   [이벤트 뜻]
     view_detail  사업 '자세히' 열람
     start_form   신청·제안 시작
     review_form  내용 확인 단계까지 작성
     email_click  '이메일로 보내기' 클릭 (이메일 작성 화면이 열린 횟수)
     copy_click   '내용 복사' 클릭

   ※ email_click 은 '보내려는 시도'이지 '실제 발송'이 아니에요.
     실제 접수 건수는 담당자 메일함에 도착한 제안 수로 확인하세요.
   ============================================================ */

var ALLOWED = ['view_detail', 'start_form', 'review_form', 'email_click', 'copy_click'];

function doPost(e) {
  try {
    var d = JSON.parse(e.postData.contents);
    if (ALLOWED.indexOf(d.event) === -1) return out('ignored');

    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(['시각', '이벤트', '사업', '제안 종류', '페이지']);
    }
    sheet.appendRow([
      new Date(),
      d.event,
      String(d.program || '').slice(0, 60),
      String(d.type || '').slice(0, 20),
      String(d.page || '').slice(0, 40)
    ]);
    return out('ok');
  } catch (err) {
    return out('error');
  }
}

function out(text) {
  return ContentService.createTextOutput(text);
}
