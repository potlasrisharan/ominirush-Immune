.PHONY: test demo docker-demo dashboard video clean

test:
	.venv/bin/python -m pytest -v tests/

demo:
	.venv/bin/python main.py --mode local --fresh

docker-demo:
	docker compose up --build

dashboard:
	.venv/bin/python main.py --serve-dashboard 8080

video:
	.venv/bin/python scripts/generate_demo_video.py

clean:
	rm -rf artifacts/ __pycache__ .pytest_cache
	git branch -D $$(git branch --list 'heal/sig-*') 2>/dev/null || true
