param(
    [Parameter(Mandatory)][string]$Python,
    [string]$HancomModule,
    [Parameter(Mandatory)][string]$BuildDirectory
)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Build = [System.IO.Path]::GetFullPath($BuildDirectory)
$null = New-Item -ItemType Directory -Path $Build -Force
$HancomArgs = @()
if ($HancomModule) {
    $HancomModule = (Resolve-Path -LiteralPath $HancomModule).Path
    if ((Get-FileHash -LiteralPath $HancomModule).Hash -ne '9AC5B97C47AC8AED1E8BCA27A3EEF39411361D8F68C262509F0C40A8F9D21BB6') {
        throw 'Check the official Hancom module.'
    }
    $HancomArgs = @('--add-binary', "$HancomModule;security")
}
& $Python "$Repo/scripts/check_public_package.py" $Repo
if ($LASTEXITCODE) { throw 'Public package check failed.' }
$Notices = Join-Path $Build 'THIRD_PARTY_NOTICES.txt'
& $Python "$Repo/scripts/collect_notices.py" $Notices
if ($LASTEXITCODE) { throw 'Third-party notice collection failed.' }
$BuildArgs = @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--name', 'HWPExamAssembler-Public',
    '--icon', "$Repo/assets/exam-assembler.ico", '--workpath', "$Build/cache", '--specpath', $Build,
    '--distpath', "$Build/dist", '--paths', $Repo, '--add-data', "$Repo/assets/exam-assembler.ico;assets",
    '--add-data', "$Repo/assets/fonts/NotoSansKR-Variable.ttf;assets/fonts", '--add-data', "$Repo/assets/fonts/OFL.txt;assets/fonts",
    '--add-data', "$Repo/project/school_profile.json;project", '--add-data', "$Repo/project/target_numbering_style.json;project",
    '--add-data', "$Repo/assets/ai-template/analysis-prompt.txt;assets/ai-template", '--add-data', "$Repo/LICENSE;.",
    '--add-data', "$Notices;.", '--hidden-import', 'pythoncom', '--hidden-import', 'pywintypes') + $HancomArgs + @("$Repo/app/main.py")
& $Python @BuildArgs *> (Join-Path $Build 'build.log')
if ($LASTEXITCODE) {
    Get-Content -LiteralPath (Join-Path $Build 'build.log') -Tail 35
    throw 'Public application build failed.'
}
Get-FileHash -LiteralPath (Join-Path $Build 'dist/HWPExamAssembler-Public.exe')
