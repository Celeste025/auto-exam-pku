# 北大在线考试自动答题

用 Playwright 登录https://exam.pku.edu.cn/examinee/exams内的考试页、自动答题；目前支持 DeepSeek api 调用答题（有参考 PDF 时采用自适应chunk选择 RAG，无参考时直接作答）。

## 快速开始（以https://exam.pku.edu.cn/examinee/exam/54  2026级研究生校规校纪考试为例）

```powershell
# 0. 进入项目目录（按你本机实际路径修改）
cd auto-exam-pku

# 1. 安装依赖（安装 chrome headless shell 时可能较慢，推荐挂代理）（如有需求可先建conda环境）
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

编辑 `.env`，必须填写 `DEEPSEEK_API_KEY`，其他无所谓，账号密码不需要填。

```powershell
# 2. 准备参考 PDF（无参考资料的场次可跳过）
#    把 PDF 放到 exams\refs\（可用下面命令，也可手动复制）。
#    若不是已有的 exam54 / exam57：在 exams\ 下新建 <场次id>.json，
#    仿照 exam54.json 填写 url 与 refs 文件名（目前仅支持 pdf），
#    之后命令里的 --exam 改成该场次 id。
New-Item -ItemType Directory -Force -Path exams\refs | Out-Null
curl.exe -L -o exams\refs\2026eg.pdf https://fresh.pku.edu.cn/fresh/Doc/2026eg.pdf

# 3. 查看场次 / 首次登录保存会话
python -m pku_exam.cli --list-exams
python -m pku_exam.cli --manual-login --exam exam54

# 4. 有参考 PDF 时先预处理（已有缓存会跳过；无 refs 的场次可跳过本步）
python -m pku_exam.cli --exam exam54 --build-index

# 5. 跑主程序自动答题并交卷
python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm --submit
```

跑前请关掉其它浏览器里同一场考试的标签页。程序默认**不交卷**；上面第 5 步因带了 `--submit` 才会交卷。

## 更多命令

```powershell
# 强制重建 RAG 索引
python -m pku_exam.cli --exam exam54 --build-index --force-rebuild

# 只做前 N 题
python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm --max-questions 5

# 答完整卷但不交卷（默认）
python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm
```


## 场次配置（增加新的答题配置）

```
exams/
  exam54.json           # 场次配置（url / refs）
  exam57.json
  refs/                 # 共享参考 PDF，把文件拷到这里
    2026eg.pdf
  exam54/               # 自动生成的 RAG 缓存，勿手改
    clean.txt
    chunks.json
```

程序会自动扫描 `exams/*.json`。首次 `--build-index` 时会创建与 json 同名的文件夹存放 RAG 缓存。

`exams/exam54.json` 示例：

```json
{
  "url": "https://exam.pku.edu.cn/examinee/exam/54/",
  "refs": ["2026eg.pdf"]
}
```

- 场次 id = 文件名（如 `exam54.json` → `exam54`）
- `refs` 写 `exams/refs/` 下的文件名；`refs: []` 或不写则走 direct（不跑 RAG）
- 多本 PDF 时当前仅索引第一本
- 新增场次：新建 `exams/<id>.json`，把 PDF 放进 `exams/refs/`，在 `refs` 里写上文件名即可

## 注意事项

- 有参考 PDF 但未预处理时，`--auto-answer` 会提示先执行 `--build-index`。
- `.env` 里也可设 `AUTO_SUBMIT=true`（效果同 `--submit`），默认请保持 `false`。
- `.env`、登录态、`exams/refs/` PDF、`exams/<id>/` 缓存默认不进 git；别人 pull 后需 `copy .env.example .env` 自行填写 API Key。

在 Cursor 帮助下完成，耗时约 3h，非常好用。(｡･∀･)ﾉﾞ
