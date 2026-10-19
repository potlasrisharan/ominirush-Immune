.PHONY: test demo docker-demo clean

test:
	.venv/bin/python -m pytest -v tests/

demo:
	.venv/bin/python main.py --mode local --fresh

docker-demo:
	docker compose up --build

clean:
	rm -rf artifacts/ __pycache__ .pytest_cache
	git branch -D $$(git branch --list 'heal/sig-*') 2>/dev/null || true
