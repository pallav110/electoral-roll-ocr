

## Run 2026-10-09 14:52:49
```
STEP 01  START - probing generate-published-pdfs
        browser launched via playwright chromium
STEP 02  Loading form URL
        attempt 1: HTTP 200
STEP 03  Form loaded OK
STEP 04  Reading captcha session state from the page
        {
  "cookiesVisible": "",
  "localStorage": "[[\"persist:root\",\"{\\\"epic\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"name\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"phoneNumber\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"userStateCd\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"userAcNo\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"userDistrictCd\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"userId\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"authToken\\\":\\\"\\\\\\\"\\\\\\\"\\\",\\\"_persist\\\":\\\"{\\\\\\\"version\\\\\\\":1,\\\\\\\"rehydrated\\\\\\\":true}\\\"}\"]]",
  "sessionStorage": "[]",
  "captchaFieldName": null,
  "captchaFieldId": null,
  "captchaFieldType": null,
  "hidden": [],
  "allInputs": [],
  "captchaImgSrcHead": null
}
STEP 05  Selecting NCT OF Delhi / 2025
STEP 06  Selecting roll type 'Final Roll - 2025'
        roll value = U05-2025-FIR
STEP 07  Selecting district 'Central'
        district value = U0505
STEP 08  Selecting constituency '23 - KAROL BAGH'
        constituency value = 23
STEP 09  Selecting language 'ENGLISH'
        available languages = ['Select Language', 'ENGLISH', 'HINDI']
        language value = ENG
STEP 10  Reading the parts table
        rows on page = 10
        checked 10 parts
        part checkbox attributes: [
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  },
  {
    "name": null,
    "value": null,
    "id": null
  }
]
STEP 11  Solving captcha
        captcha image taken from inline data URI
        vision attempt 1 = 'geevje'
STEP 12  Submitting with captcha 'geevje'
STEP 13  REQ -> POST https://gateway-voters.eci.gov.in/api/v1/printing-publish/generate-published-pdfs
        headers: {
  "sec-ch-ua-platform": "\"Linux\"",
  "platform-type": "ECIWEB",
  "referer": "",
  "sec-ch-ua": "\"HeadlessChrome\";v=\"153\", \"Not_A Brand\";v=\"8\", \"Chromium\";v=\"153\"",
  "sec-ch-ua-mobile": "?0",
  "applicationname": "VSP",
  "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.8010.12 Safari/537.36",
  "channelidobo": "VSP",
  "content-type": "application/json"
}
        POST BODY: None
STEP 14  Waiting 30s for the response
STEP 15  Reading any visible error the site displayed
STEP 16  RES <- 400 https://gateway-voters.eci.gov.in/api/v1/printing-publish/generate-published-pdfs
        request headers sent by browser:
           sec-ch-ua-platform: "Linux"
           platform-type: ECIWEB
           referer: 
           sec-ch-ua: "HeadlessChrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"
           sec-ch-ua-mobile: ?0
           applicationname: VSP
           user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.8010.12 Safari/537.36
           channelidobo: VSP
           content-type: application/json
        RESPONSE BODY (104 bytes): b'{"status":"Failed","statusCode":400,"refId":null,"message":"Invalid Catpcha","payload":null,"file":null}'
        PARSED JSON: {
  "status": "Failed",
  "statusCode": 400,
  "refId": null,
  "message": "Invalid Catpcha",
  "payload": null,
  "file": null
}
        content-type: application/json
        content-disposition: None
        content-length: 104
STEP 17  Screenshot written to step_after_submit.png
STEP 18  1 generate-published-pdfs request(s) seen
```
