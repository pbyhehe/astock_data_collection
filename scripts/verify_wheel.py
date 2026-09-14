"""Install a built wheel in a disposable target and run the command verifier."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel',type=Path)
    args=parser.parse_args()
    wheel=args.wheel.resolve(strict=True)
    root=Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix='astock-wheel-check-') as temporary:
        directory=Path(temporary)
        target=directory/'installed'
        subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-index',
                        '--target',str(target),str(wheel)],check=True,timeout=90)
        env=dict(os.environ,PYTHONPATH=str(target),PYTHONSAFEPATH='1')
        code='''import astock, pathlib, runpy, sys
from importlib.metadata import version
assert pathlib.Path(astock.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())
assert astock.__version__ == version('astock-raw') == '0.2.0'
print('verified wheel import:', astock.__file__)
runpy.run_path(sys.argv[2],run_name='__main__')
'''
        subprocess.run([sys.executable,'-c',code,str(target),str(root/'scripts/verify_commands.py')],
                       cwd=directory,env=env,check=True,timeout=120)
    print('Wheel 0.2.0 verified; temporary installation and databases removed; dependencies reused from active interpreter.')


if __name__=='__main__':
    main()
