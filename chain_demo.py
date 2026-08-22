import subprocess
def run_cmd(cmd):
    return subprocess.run(cmd, shell=True).returncode
