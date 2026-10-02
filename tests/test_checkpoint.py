import os
from pathlib import Path
import subprocess
import tempfile
import unittest

class CheckpointTests(unittest.TestCase):
    def git(self, folder, *args):
        return subprocess.run(['git','-C',str(folder),*args],check=True,capture_output=True,text=True).stdout

    def check_race(self, conflict):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);remote=root/'remote.git';first=root/'first';worker=root/'worker'
            self.git(root,'init','--bare','--initial-branch=main',str(remote))
            self.git(root,'clone',str(remote),str(first))
            self.git(first,'config','user.name','Checkpoint Test')
            self.git(first,'config','user.email','test@example.invalid')
            (first/'state.json').write_text('{"generation":0}\n')
            self.git(first,'add','.');self.git(first,'commit','-m','initial');self.git(first,'push','origin','main')
            self.git(root,'clone',str(remote),str(worker))
            changed='state.json' if conflict else 'collector.py'
            value='{"generation":2}\n' if conflict else '# repaired collector\n'
            (first/changed).write_text(value)
            self.git(first,'add','.');self.git(first,'commit','-m','concurrent update');self.git(first,'push','origin','main')
            expected=self.git(remote,'rev-parse','main')
            (worker/'state.json').write_text('{"generation":1}\n')
            lines=(Path(__file__).parents[1]/'.github/workflows/monitor.yml').read_text().splitlines()
            start=next(i for i,l in enumerate(lines) if 'id: checkpoint' in l)
            start=next(i for i in range(start,len(lines)) if lines[i].strip()=='run: |')+1
            commands=[]
            for line in lines[start:]:
                if not line.startswith('          '):break
                commands.append(line.strip())
            result=subprocess.run(['bash','-e','-c','\n'.join(commands)],cwd=worker,env=dict(os.environ,DEFAULT_BRANCH='main'),capture_output=True,text=True)
            if conflict:
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.git(remote,'rev-parse','main'),expected)
                self.assertEqual(self.git(remote,'show','main:state.json'),value)
            else:
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(self.git(remote,'show','main:state.json'),'{"generation":1}\n')
                self.assertEqual(self.git(remote,'show','main:collector.py'),value)

    def test_code_update_preserves_code_and_checkpoint(self):self.check_race(False)
    def test_state_conflict_does_not_overwrite_remote(self):self.check_race(True)
