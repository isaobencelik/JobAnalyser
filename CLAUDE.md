Never execute anything before asking.

You develop in the Linkedin&Glassdoor folder — server.py, db.py, paths.py, job-analyser.html are the single source of truth. We keep editing and testing those with start.bat / python server.py, same as always.

When you run build.bat, PyInstaller reads those source files and writes the exe to dist\JobAnalyser.exe. It also drops a build\ temp folder and a JobAnalyser.spec file — both are just build scratch, ignore them (don't edit anything in dist\ or build\; they're outputs, not source).