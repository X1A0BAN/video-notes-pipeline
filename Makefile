.PHONY: help install test cov check scan run dry clean

help:
	@echo "install  安装依赖（含开发依赖）"
	@echo "test     跑测试"
	@echo "cov      跑测试并出覆盖率"
	@echo "check    环境自检"
	@echo "scan     列出待处理视频"
	@echo "run      跑完整流水线"
	@echo "dry      用占位笔记跑通流程（不需要模型密钥）"
	@echo "clean    清理运行时产物"

install:
	pip install -e ".[dev]"

test:
	PYTHONPATH=src pytest tests -q

cov:
	PYTHONPATH=src pytest tests --cov=vnotes --cov-report=term-missing

check:
	PYTHONPATH=src python -m vnotes.cli check

scan:
	PYTHONPATH=src python -m vnotes.cli scan

run:
	PYTHONPATH=src python -m vnotes.cli run

dry:
	PYTHONPATH=src python -m vnotes.cli run --dry-run

clean:
	rm -rf output/* .pytest_cache .coverage htmlcov
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
