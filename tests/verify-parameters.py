#!/usr/bin/env python3
"""在独立 Jenkins 实例验证实际版本持久化、历史回填及其它参数保留。"""
import argparse
import base64
import datetime as dt
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from jenkins_api import JenkinsAPI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text())
    assert cfg.get('verificationInstance') is True and cfg['namespace'] != 'jenkins', '需要独立验收实例'
    state = ROOT / '.state' / cfg['namespace']
    users = json.loads((state / 'users.json').read_text())
    admin = JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'], job='open5gs')
    builder = JenkinsAPI(cfg['jenkinsUrl'], 'builder1', users['BUILDER1_PASSWORD'], job='open5gs')
    original = json.loads(admin.script("println(groovy.json.JsonOutput.toJson(jenkins.model.Jenkins.get().getItem('open5gs').definition.script));return null"))
    expected_source = '\n'.join((ROOT / 'jenkins' / name).read_text() for name in ['storage.groovy', 'compiler.groovy', 'pipeline.groovy'])
    assert original == expected_source, '部署脚本与当前仓库不一致'
    expected_offline = cfg['open5gs'].get('offline', True)
    report = {'namespace': cfg['namespace'], 'offline': expected_offline, 'checks': []}
    numbers = []

    def record(name, details):
        report['checks'].append(dict(name=name, status='PASS', details=details))
        (state / 'parameters-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS:', name, flush=True)

    def script(text):
        encoded = base64.b64encode(text.encode()).decode()
        return admin.script("new GroovyShell(jenkins.model.Jenkins.get().pluginManager.uberClassLoader,new Binding([out:out])).evaluate(new String('" + encoded + "'.decodeBase64(),'UTF-8'));return null")

    cases = [({}, None, None), ({'CHART_VERSION': '', 'APP_VERSION': ''}, None, None),
             ({'CHART_VERSION': ' \t ', 'APP_VERSION': ' \t '}, None, None),
             ({'CHART_VERSION': '2.3.4-rc.1', 'APP_VERSION': '26.11.06-custom'}, '2.3.4-rc.1', '26.11.06-custom'),
             ({'CHART_VERSION': '4.5.6', 'APP_VERSION': ''}, '4.5.6', None),
             ({'CHART_VERSION': ' 5.6.7 ', 'APP_VERSION': ' release-test '}, '5.6.7', 'release-test')]
    try:
        definitions = admin.get(admin.job_path + 'api/json?tree=property[parameterDefinitions[name]]')
        names = [p['name'] for prop in definitions['property'] for p in prop.get('parameterDefinitions', [])]
        assert 'OFFLINE' not in names and len(names) == 6, names
        marker = "lock(label: 'xran-build-slot', quantity: 1) {"
        assert marker in original
        admin.set_pipeline(original.replace(marker, marker + "\n echo('parameter-verification-only'); return\n", 1))
        for values, chart, app in cases:
            # An old client supplying the opposite mode cannot override site configuration.
            number = builder.wait_number(builder.trigger(dict(REF_TYPE='branch', GIT_REF='main',
                OFFLINE=str(not expected_offline).lower(), **values)))
            numbers.append(number)
            build = builder.wait(number, timeout=120)
            assert build['result'] == 'SUCCESS', builder.console(number)
            submitted = {p['name']: p['value'] for action in build['actions'] for p in action.get('parameters', [])}
            started = dt.datetime.fromtimestamp(build['timestamp'] / 1000, ZoneInfo('Asia/Shanghai'))
            date_version = f'{started.year % 100:02d}.{started.month}.{started.day}'
            assert submitted['CHART_VERSION'] == (chart or date_version), submitted
            assert submitted['APP_VERSION'] == (app or date_version), submitted
            assert 'OFFLINE' not in submitted and submitted['GIT_REF'] == 'main'
            metadata = json.loads(admin.script("println(new File(jenkins.model.Jenkins.get().getItem('open5gs').getBuildByNumber(" +
                str(number) + ").rootDir,'xran-metadata.json').text);return null"))
            assert metadata['offline'] is expected_offline, metadata
            assert submitted['COMPILER_IMAGE_PULL_POLICY'] == 'Always'
            assert any(cause.get('userId') == 'builder1' for action in build['actions'] for cause in action.get('causes', []))
            record('API 实际参数保存及后台构建模式 #' + str(number), {'parameters': submitted, 'offline': metadata['offline']})

        support = '\n'.join((ROOT / 'jenkins' / name).read_text() for name in ['storage.groovy', 'compiler.groovy'])
        fixtures = ','.join(str(n) for n in numbers[:3] + [numbers[4]])
        migration = script(support + '''
def job = jenkins.model.Jenkins.get().getItem('open5gs')
def runs = [''' + fixtures + '''].collect { job.getBuildByNumber(it) }
def reset = { run, values ->
    run.replaceAction(run.getAction(hudson.model.ParametersAction).createUpdated(values.collect { name, value ->
        new hudson.model.StringParameterValue(name, value)
    }))
    run.save()
}
runs.take(3).each { reset(it, [CHART_VERSION: '', APP_VERSION: '']) }
def historical = new File(runs[0].rootDir, 'xran-metadata.json')
def meta = XranStorage.parseJson(historical.text)
meta.chartVersion = '25.1.2'; meta.appVersion = '25.1.2-release'
historical.text = groovy.json.JsonOutput.toJson(meta)
new File(runs[1].rootDir, 'xran-metadata.json').delete() // No trustworthy version to recover.
reset(runs[3], [APP_VERSION: '']) // An explicit Chart version must remain intact.
def mixed = new File(runs[3].rootDir, 'xran-metadata.json')
def mixedMeta = XranStorage.parseJson(mixed.text)
mixedMeta.chartVersion = '99.99.99'; mixedMeta.appVersion = '25.3.4'
mixed.text = groovy.json.JsonOutput.toJson(mixedMeta)
def before = runs.collect { it.getAction(hudson.model.ParametersAction).allParameters.findAll {
    !(it.name in ['CHART_VERSION', 'APP_VERSION'])
}.collectEntries { [(it.name): [it.class.name, it.value]] } }
XranCompiler.restoreVersions(job)
def values = runs.collect { it.getAction(hudson.model.ParametersAction).allParameters.collectEntries { [(it.name): it.value] } }
assert values[0].CHART_VERSION == '25.1.2' && values[0].APP_VERSION == '25.1.2-release'
assert values[1].CHART_VERSION == '' && values[1].APP_VERSION == ''
assert values[2].CHART_VERSION && values[2].APP_VERSION
assert values[3].CHART_VERSION == '4.5.6' && values[3].APP_VERSION == '25.3.4'
assert XranCompiler.restoreVersions(job) == 0
assert before == runs.collect { it.getAction(hudson.model.ParametersAction).allParameters.findAll {
    !(it.name in ['CHART_VERSION', 'APP_VERSION'])
}.collectEntries { [(it.name): [it.class.name, it.value]] } }
runs.each { run ->
    run.reload() // Check persistence to build.xml rather than only the in-memory action.
}
assert runs[0].getAction(hudson.model.ParametersAction).getParameter('CHART_VERSION').value == '25.1.2'
println(groovy.json.JsonOutput.toJson([numbers:runs.collect{it.number}, versions:values,
    explicitPreserved:true, missingMetadataSkipped:true, idempotent:true, reloadVerified:true]))
return null
''')
        record('按历史元数据回填、显式值保护、缺失元数据跳过、幂等及磁盘重载', json.loads(migration))
        report['status'] = 'PASS'
        (state / 'parameters-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    finally:
        for number in numbers:
            if builder.get(builder.job_path + f'{number}/api/json')['building']:
                admin.post(admin.job_path + f'{number}/stop')
                admin.wait(number, timeout=120)
        admin.set_pipeline(original)


if __name__ == '__main__':
    main()
