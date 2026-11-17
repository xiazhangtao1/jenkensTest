"""Trusted project recipes, shared by deployment and the build adapter."""
import copy
import hashlib
import json
from pathlib import PurePosixPath, Path
import re


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('项目路径非法')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('项目路径越界: ' + value)
    return value


def recipe(value):
    relative(value['workdir'])
    command = value['command']
    if not isinstance(command, list) or not command or any(not isinstance(a, str) or not a or '\x00' in a for a in command):
        raise ValueError('command 必须是非空参数数组')
    # Only known literal substitutions; never shell-evaluate administrator recipes.
    for arg in command:
        arg.format(target='target', imageTag='tag')
    environment(value.get('env', {}))
    if 'artifacts' in value:
        if not isinstance(value['artifacts'], list) or not value['artifacts']:
            raise ValueError('必须配置预期产物')
        for name in value['artifacts']:
            relative(name)
            if not re.fullmatch(r'[A-Za-z0-9_./-]+\.tar\.gz', name):
                raise ValueError('通用服务产物必须为明确的 .tar.gz 路径')
        if len(set(value['artifacts'])) != len(value['artifacts']):
            raise ValueError('重复产物')
        if len({PurePosixPath(n).name for n in value['artifacts']}) != len(value['artifacts']):
            raise ValueError('产物文件名不能重复')


def environment(value):
    if not isinstance(value, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(item, str) or '\x00' in item for key, item in value.items()):
        raise ValueError('env 必须为环境变量名到字符串的映射')


def load_registry(site, path):
    import yaml
    data = yaml.safe_load(Path(path).read_text())
    if data.get('format') != 1 or not isinstance(data.get('projects'), dict):
        raise ValueError('项目注册表格式错误')
    projects = copy.deepcopy(data['projects'])
    for name, overrides in site.get('projects', {}).items():
        if name not in projects:
            raise ValueError('站点覆盖了未注册项目: ' + name)
        projects[name].update(copy.deepcopy(overrides))
    for name, spec in projects.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,61}', name) or name == 'storage-maintenance':
            raise ValueError('项目任务名非法: ' + name)
        adapter = spec.get('adapter')
        if adapter not in ('xran', 'xcn', 'command'):
            raise ValueError('不支持的项目适配器: ' + str(adapter))
        spec.setdefault('enabled', True)
        if not isinstance(spec['enabled'], bool):
            raise ValueError('enabled 必须为布尔值')
        spec.setdefault('defaultRef', 'main')
        spec.setdefault('displayName', name)
        environment(spec.get('env', {}))
        if adapter in ('xran', 'xcn'):
            legacy = site if adapter == 'xran' else site['xcn']
            for key in ('gitUrl', 'gitCredentialsId', 'registry', 'registryCredentialsId', 'imageRepositories'):
                if key in legacy:
                    spec.setdefault(key, copy.deepcopy(legacy[key]))
            if adapter == 'xran':
                spec.setdefault('services', site['services'])
                spec.setdefault('images', site['images'])
            for build in spec['builds'].values():
                recipe(build)
                if 'artifactDirectory' in build:
                    relative(build['artifactDirectory'])
        else:
            if not isinstance(spec.get('targets'), dict) or not spec['targets']:
                raise ValueError('项目必须有服务目标')
            for target, build in spec['targets'].items():
                if not re.fullmatch(r'[A-Za-z0-9_.-]+', target):
                    raise ValueError('服务目标非法')
                recipe(build)
                if not build.get('artifacts'):
                    raise ValueError('缺少预期产物')
        if not spec.get('gitUrl', '').startswith(('ssh://', 'https://', 'http://', 'git@')):
            raise ValueError('项目 Git URL 非法: ' + name)
        spec.setdefault('gitCredentialsId', '')
        spec['id'] = name
        spec['rulesSha256'] = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    return projects
