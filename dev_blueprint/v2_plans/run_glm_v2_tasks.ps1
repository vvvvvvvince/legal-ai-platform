# Legal AI V2.0 — 本机 GLM CLI 执行脚本
# 用法：在 PowerShell 中运行
#   cd "C:\Users\lidongye\Desktop\Codex Projects\legal-ai-platform"
#   .\dev_blueprint\v2_plans\run_glm_v2_tasks.ps1
#
# 前置条件：
#   1. 已安装 glm CLI（glm --version 可用）
#   2. 已配置 API Key：glm init -g  或编辑 %USERPROFILE%\.glm\config.json

$ErrorActionPreference = "Stop"

$ProjectRoot = "C:\Users\lidongye\Desktop\Codex Projects\legal-ai-platform"
$Blueprint   = "dev_blueprint\v2_plans\project_blueprint_v2_for_codex.md"
$Plan        = "dev_blueprint\v2_plans\implementation_plan.md"

if (-not (Test-Path $ProjectRoot)) {
    Write-Error "项目路径不存在: $ProjectRoot"
    exit 1
}

Set-Location $ProjectRoot
Write-Host "项目目录: $(Get-Location)" -ForegroundColor Cyan

if (-not (Get-Command glm -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 glm 命令，请先安装并配置 GLM CLI"
    exit 1
}

function Invoke-GlmTask {
    param(
        [string]$Name,
        [string]$Profile,
        [string]$Prompt
    )
    Write-Host ""
    Write-Host "========== $Name ==========" -ForegroundColor Yellow
    glm --query $Prompt --profile $Profile
    if ($LASTEXITCODE -ne 0) {
        Write-Error "GLM 任务失败: $Name"
        exit $LASTEXITCODE
    }
}

# --- Task 1: 后端审查 Prompt 升级与合同分类 ---
Invoke-GlmTask -Name "Task 1: 后端合同类型识别" -Profile "api-integration" -Prompt @"
你是 Legal AI 项目的后端工程师。请严格按 $Blueprint 中 Task 1 实现：

1. 在 backend/app/schemas/review.py 的 ReviewResponse 增加 contract_type: str | None 字段
2. 升级 backend/app/services/openai_review.py：
   - 重构 SYSTEM_PROMPT，第一步识别合同类型（劳动/采购/房屋租赁/通用）
   - 按类型应用不同审查规则
   - 约束 original_text 与 insert_after_text 锚点准确性
   - 更新 _normalize_review_payload 保留 contract_type
3. 只修改必要文件，匹配现有代码风格
4. 完成后说明改了哪些文件
"@

# --- Task 2a: 模糊匹配算法 ---
Invoke-GlmTask -Name "Task 2a: 模糊匹配算法" -Profile "frontend-design" -Prompt @"
你是 Legal AI 项目的前端工程师。请按 $Blueprint Task 2 第 1 节实现：

1. 在 frontend/src/reviewUtils.ts 实现 findFuzzyMatch、editDistance、getSimilarity
2. 支持精确匹配失败时用段落级编辑距离（默认阈值 0.8）
3. 在 App.tsx 中引用该函数替换原有定位逻辑
4. 保持 TypeScript 类型完整
"@

# --- Task 2b: 对比痕迹渲染与布局优化 ---
Invoke-GlmTask -Name "Task 2b: 对比痕迹与布局" -Profile "frontend-design" -Prompt @"
你是 Legal AI 项目的前端工程师。请按 $Blueprint Task 2 第 2-4 节和 $Plan 前端部分实现：

1. App.tsx：DeleteMark / InsertMark Tiptap 扩展，引用修改时红删绿增高亮
2. 缺失条款手动定位下拉面板，支持段落快捷追加
3. 【...】占位符 Local Linting（placeholder-lint-mark）
4. 审查完成后上传区折叠为 .compact-document-bar
5. 右侧栏可折叠，.workspace-collapsed + .editor-page-focus 800px 聚焦模式
6. styles.css 补充 .del-mark .ins-mark .placeholder-lint-mark .compact-document-bar 等样式
"@

# --- Task 3: OpenXML 修订痕迹导出 ---
Invoke-GlmTask -Name "Task 3: Word 修订痕迹导出" -Profile "api-integration" -Prompt @"
你是 Legal AI 项目的后端工程师。请按 $Blueprint Task 3 实现：

1. 重构 backend/app/services/docx_modifier.py 的 modify_docx_inplace
2. 替换修改：原文包 w:del，建议包 w:ins，带 w:author 和 w:date
3. 缺失条款【缺失该约定】用 w:ins 原生追加
4. 在 word/settings.xml 写入 w:trackRevisions 开启全局修订追踪
5. 保持与现有 export API 兼容
"@

# --- Task 4: 自动化测试 ---
Invoke-GlmTask -Name "Task 4: 自动化测试" -Profile "api-integration" -Prompt @"
你是 Legal AI 项目的测试工程师。请按 $Blueprint 第三节验证要求：

1. frontend/tests/review-utils.test.mjs：为 findFuzzyMatch 增加多样本用例（标点漂移、漏字、标题匹配）
2. backend/tests/：扩充 OpenXML 修订测试，断言 w:ins、w:del、w:trackRevisions
3. 确保 python -m pytest backend -v 与 cd frontend && npm test 可通过
"@

Write-Host ""
Write-Host "========== 本地验证 ==========" -ForegroundColor Green
Write-Host @"
请在项目根目录手动执行：

  cd backend
  .venv\Scripts\Activate.ps1
  pip install -r requirements-dev.txt
  cd ..
  python -m pytest backend -v

  cd frontend
  npm install
  npm test
  npx tsc --noEmit
  npm run dev

浏览器打开 http://localhost:5173，上传劳动合同与采购合同，验证合同类型、红绿对比、Word 修订导出。
"@ -ForegroundColor Gray
