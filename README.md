# Research Pipeline

Equity research automation CLI tool.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
playwright install chromium
```

### API Keys

Set your Anthropic API key for document classification:

```bash
# Windows (PowerShell)
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# Windows (cmd)
set ANTHROPIC_API_KEY=sk-ant-...

# Linux/macOS
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
# Download IR materials (auto-classifies after download)
python main.py download-materials AAPL
python main.py download-materials "2330 TT" --since 2020 --limit 50

# Classify documents standalone
python main.py classify AAPL
python main.py classify AAPL --reclassify

# Build Excel model
python main.py model AAPL

# Analyse a specific file
python main.py analyse AAPL --input data/AAPL/ir/10-K.pdf

# Add a new company
python main.py add-company
```
