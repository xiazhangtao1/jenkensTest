def cfg = XranStorage.parseJson(new File('/var/xran/site.json').text)
def references = ['192.168.10.35/xnet/xran-compile:latest',
    'registry.example:5000/team/compiler:26.11.05', 'example/compiler@sha256:' + ('a' * 64)]
def policies = ['Always', 'IfNotPresent', 'Never']
int checked = 0
int resourceCases = 0
['service', 'image', 'xcn'].findAll { templates.containsKey(it) }.each { kind ->
    // The caller provides rendered JSON; the live deployment can use the previous version.
    def text = templates[kind]
    def original = XranStorage.parseJson(text)
    references.each { reference ->
        policies.each { policy ->
            def job = kind == 'xcn' ? 'xcn' : 'x-ran-cloud'
            def pod = XranCompiler.template(text, reference, policy, '123', job)
            def builder = pod.spec.containers.find { it.name == 'builder' }
            assert builder.image == reference && builder.imagePullPolicy == policy
            assert pod.metadata.labels['xran-build'] == 'b123'
            assert pod.metadata.labels['xran-job'] == job
            assert pod.spec.containers.findAll { it.name != 'builder' } == original.spec.containers.findAll { it.name != 'builder' }
            assert builder.resources == original.spec.containers.find { it.name == 'builder' }.resources
            assert XranStorage.parseJson(text) == original
            checked++
        }
    }
    def settings = io.xran.jenkins.BuildPlatformConfiguration.get().form
    def allocation = io.xran.jenkins.PlatformSettings.allocation(settings, kind)
    def snapshot = [settingsVersion: 123, resourceProfile: kind, resourceAllocation: allocation,
        config: io.xran.jenkins.PlatformSettings.runtime(settings, kind)]
    def defaultPod = XranCompiler.template(text, references[0], 'Never', '124',
        kind == 'xcn' ? 'xcn' : 'x-ran-cloud', snapshot)
    defaultPod.spec.containers.each { container ->
        assert container.resources == original.spec.containers.find { it.name == container.name }.resources
    }
    settings[kind + 'Cpu'] = 6
    settings[kind + 'MemoryGiB'] = 12
    settings[kind + 'TemporaryStorageGiB'] = 7
    snapshot.resourceAllocation = io.xran.jenkins.PlatformSettings.allocation(settings, kind)
    snapshot.config = io.xran.jenkins.PlatformSettings.runtime(settings, kind)
    def changed = XranCompiler.template(text, references[0], 'Never', '125',
        kind == 'xcn' ? 'xcn' : 'x-ran-cloud', snapshot)
    assert changed.spec.containers.sum { (it.resources.limits.cpu - 'm') as int } == 6000
    assert changed.spec.containers.sum { (it.resources.limits.memory - 'Mi') as int } == 12 * 1024
    assert changed.spec.containers.sum { (it.resources.limits['ephemeral-storage'] - 'Mi') as int } == 7 * 1024
    assert changed.spec.volumes.find { it.name == 'workspace-volume' }.emptyDir.sizeLimit == '7Gi'
    assert changed.spec.containers.findAll { it.name == 'dind' }.every { it.securityContext.privileged }
    assert changed.spec.automountServiceAccountToken == false
    assert XranStorage.parseJson(text) == original
    resourceCases++
}
['', 'ubuntu', 'ubuntu:latest\nprivileged: true', 'repo/sdk:bad;touch /tmp/escape', '-repo/sdk:tag'].each { invalid ->
    boolean rejected = false
    try { XranCompiler.validate(invalid, 'Always') } catch (IllegalArgumentException expected) { rejected = true }
    assert rejected: invalid
}
boolean rejected = false
try { XranCompiler.validate(references[0], 'Sometimes') } catch (IllegalArgumentException expected) { rejected = true }
assert rejected
println groovy.json.JsonOutput.toJson([status: 'PASS', templateCases: checked, resourceCases: resourceCases, invalidReferencesRejected: 5, invalidPolicyRejected: true])
