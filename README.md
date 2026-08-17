# 北大在线考试自动答题

用 Playwright 登录https://exam.pku.edu.cn/examinee/exams内的考试页、自动答题；目前支持 DeepSeek api 调用答题（有参考 PDF 时采用自适应chunk选择 RAG，无参考时直接作答）。

## 快速开始

```powershell
# 1. 安装依赖（安装chrome headless shell时速度较慢，推荐挂代理）
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

编辑 `.env`，必须填写 `DEEPSEEK_API_KEY`（可选填 `EXAM_ID=exam54`）。账号密码不需要填。

```powershell
# 2. 查看场次 / 首次登录保存会话
python -m pku_exam.cli --list-exams
python -m pku_exam.cli --manual-login --exam exam54

# 3. 有参考 PDF 时先预处理（已有缓存会跳过；无 refs 的场次可跳过本步）
python -m pku_exam.cli --exam exam54 --build-index

# 4. 跑主程序自动答题并交卷
python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm --submit
```

跑前请关掉其它浏览器里同一场考试的标签页。程序默认**不交卷**；上面第 4 步因带了 `--submit` 才会交卷。

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
  exam54/
    exam.json           # url / refs
    refs/*.pdf          # 参考资料（可空）
    rag/                # --build-index 生成，勿手改
```

程序会自动扫描 `exams/*/exam.json`，无需额外索引文件。

`exam.json` 示例：

```json
{
  "id": "exam54",
  "name": "2026级研究生校规校纪考试",
  "url": "https://exam.pku.edu.cn/examinee/exam/54/",
  "refs": ["refs/2026s.pdf"],
  "rag_dir": "rag"
}
```

- `name` 可省略或留空，缺省用目录名（如 `exam54`）
- `refs` 只识别 PDF；`refs: []` 或无可读 PDF 时走 direct（不跑 RAG）
- 多本 PDF 时当前仅索引第一本
- 新增场次：新建 `exams/<id>/`，放入 `exam.json`（及可选 `refs/`）即可

## 注意事项

- 有参考 PDF 但未预处理时，`--auto-answer` 会提示先执行 `--build-index`。
- `.env` 里也可设 `AUTO_SUBMIT=true`（效果同 `--submit`），默认请保持 `false`。
- `.env`、登录态、`refs` PDF、`rag/` 缓存默认不进 git；别人 pull 后需 `copy .env.example .env` 自行填写 API Key。

在 Cursor 帮助下完成，非常好用。
