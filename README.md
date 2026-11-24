# HWPExamAssembler 공용판

교사가 작성한 HWP·HWPX 문항을 학교 시험지 양식에 모으는 Windows 앱입니다. 한글 화면에서 머리말과 유형 안내처럼 남길 부분을 확인하고, 새 문항을 넣을 위치를 등록합니다. 수합본을 옆에 열어 두고 앱에서 순서와 난이도를 편집할 수 있습니다.

**[Windows용 최신 공용판 다운로드 (.exe)](https://github.com/CauchyRiemannEquations/HWPExamAssembler/releases/latest/download/HWPExamAssembler-Public.exe)**

한컴오피스 한글이 설치된 Windows PC에서 내려받은 `HWPExamAssembler-Public.exe`를 더블클릭하세요. Python 설치나 압축 해제는 필요 없습니다. 문서를 열 때 한글이 파일 접근을 물으면 선택한 파일을 확인하고 허용하세요. [릴리스 안내 및 체크섬](https://github.com/CauchyRiemannEquations/HWPExamAssembler/releases/latest)

## 처음 사용하는 교사

1. Windows에 설치된 한컴오피스 한글을 준비합니다. 공용판 홈에서 **내 학교 양식 선택…**을 누르고 양식 파일을 엽니다.
2. 항목을 클릭하면 한글 작업 사본에 해당 위치가 표시됩니다. 틀린 범위나 삽입점만 한글에서 다시 짚고 **등록하고 사용**을 누릅니다. 등록한 양식은 다음 수합에서 바로 선택할 수 있습니다.
3. 교사의 출제 파일들을 추가하고 새 수합본을 저장합니다. 편집 화면에서 문항 순서와 난이도를 바꾼 뒤 **한글에서 확인**으로 결과를 검토합니다.

이 과정은 인터넷 연결 없이 이용할 수 있습니다. 복잡한 양식에서 영역 제안을 한 번 더 받고 싶을 때만 **AI로 영역 제안…**을 누릅니다. **보낼 내용 보기**에서 문단 번호와 표·수식·문항 번호 같은 구조 단서를 확인할 수 있습니다. [사용 안내](사용안내.txt) · [문서 및 계정 정보 처리](PRIVACY.md)

| 양식 등록에서 선택 | 사용 방법 |
| --- | --- |
| ChatGPT | **ChatGPT로 계속**을 누르고 기본 브라우저에서 로그인합니다. 지원되는 Plus·Pro 계정으로 요금제 사용을 승인하면 영역 제안을 받을 수 있습니다. 교사가 API 키를 만들 필요가 없습니다. [OpenAI 공식 안내](https://developers.openai.com/siwc/quickstart) |
| Gemini | 기관의 Google 설정을 준비한 PC에서 **Google로 계속**을 누릅니다. 분석에는 연결한 Google Cloud 프로젝트의 Gemini API 할당량이 적용됩니다. [Google 공식 안내](https://ai.google.dev/gemini-api/docs/oauth) |
| 웹에서 직접 사용 | **웹에서 복사·붙여넣기로 분석**을 선택합니다. 요청을 복사해 ChatGPT 또는 Gemini 대화에 붙여넣고 답변을 앱으로 가져옵니다. |

AI가 제안한 경계는 변경 전후 위치를 확인하고 **제안 적용**을 눌러야 등록 화면에 반영됩니다. 이미 직접 짚은 위치와 겹치는 범위는 앱이 검사합니다.

## 소스에서 실행

Windows, 설치형 한컴오피스 한글과 Python 3.12가 필요합니다. 저장소 루트에서 PowerShell로 실행합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.main
```

Windows 단일 실행 파일을 만들려면 같은 디렉터리에서 다음 명령을 사용합니다. 결과는 `build\dist\HWPExamAssembler-Public.exe`에 생성됩니다.

```powershell
.\scripts\build_public.ps1 -Python .\.venv\Scripts\python.exe -BuildDirectory .\build
```

한컴 보안 모듈을 포함하려면 한컴의 **보안모듈(Automation).zip**에서 `FilePathCheckerModuleExample.dll`을 받아 빌드 인수 `-HancomModule`로 경로를 지정합니다. 스크립트가 지원하는 공식 모듈의 SHA-256을 확인합니다. 모듈을 생략한 환경에서는 한글의 파일 접근 안내에 따라 승인할 수 있습니다. [한컴 오토메이션 개발 가이드와 이용 조건](https://developer.hancom.com/en-us/hwpautomation)

## 기관에서 Gemini 연결하기

1. Google Cloud 프로젝트에서 **Generative Language API**를 사용 설정하고 OAuth 동의 화면을 구성합니다. 테스트 중에는 허용된 계정을 등록하고, 외부 공개 시에는 Google의 동의 화면·검증 요건을 확인합니다. [Google OAuth 설정](https://ai.google.dev/gemini-api/docs/oauth)
2. OAuth 클라이언트 유형으로 **데스크톱 앱**을 선택해 JSON을 받습니다. 사용자는 같은 프로젝트에 `serviceusage.services.use` 권한이 있어야 합니다. 관리자는 참여 교사 또는 교사 그룹에 이 권한을 포함하는 **Service Usage Consumer** 역할을 부여할 수 있습니다. [Google Cloud 프로젝트 사용 권한](https://docs.cloud.google.com/docs/quotas/set-quota-project)
3. 실행 파일 옆에 `google-desktop-client.json` 이름으로 두거나 **기관용 Google 설정 파일 선택…**에서 파일을 지정합니다. 소스 실행 시에는 저장소 루트에 두거나 `HWP_EXAM_GOOGLE_CLIENT` 환경 변수로 경로를 지정할 수 있습니다. 관리자가 이 설정을 배포하면 교사는 자기 Google 계정으로 로그인합니다.

## 검증과 이전 버전 자료

실제 한글에서 가상 학교 양식을 등록하고 HWP·HWPX 원고를 수합했습니다. 순서·난이도 변경, 저장과 재개방 뒤 문항별 수식 9개와 고정 표·안내문을 확인했습니다. 로그인과 분석의 자동 테스트는 로컬 브라우저 콜백 및 모의 제공자 응답을 사용합니다. 실제 계정의 동의와 사용 한도는 각 제공자가 결정합니다.

v0.2.0-alpha의 43개 항목 서식 검토, 수합 후 서식 대조와 외부 JSON 규격은 별도 자료로 보관합니다. [서식 기준과 한계](docs/template-format-review.md) · [서식 규칙 JSON](docs/ai-template-profile.md) · [분석 프롬프트](ai-template/analysis-prompt.txt) · [당시 검증 기록](docs/verification.json)

## 라이선스

프로젝트 자체 소스에는 MIT 라이선스를 적용합니다. 번들 글꼴 Noto Sans KR에는 SIL Open Font License 1.1이 적용됩니다. 한컴 한글·공식 DLL과 Python 패키지에는 각 공급자의 라이선스가 적용됩니다. [프로젝트 라이선스](LICENSE) · [글꼴 라이선스](assets/fonts/OFL.txt)
