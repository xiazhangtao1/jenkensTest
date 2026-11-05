def cfg = XranStorage.parseJson(new File('/var/xran/site.json').text)
def references = ['192.168.10.35/xnet/xran-compile:latest',
    'registry.example:5000/team/compiler:26.11.05', 'example/compiler@sha256:' + ('a' * 64)]
def policies = ['Always', 'IfNotPresent', 'Never']
int checked = 0
['service', 'image'].each { kind ->
    // The caller provides rendered JSON; the live deployment can use the previous version.
    def text = templates[kind]
    def original = XranStorage.parseJson(text)
    references.each { reference ->
        policies.each { policy ->
            def pod = XranCompiler.template(text, reference, policy, '123')
            def builder = pod.spec.containers.find { it.name == 'builder' }
            assert builder.image == reference && builder.imagePullPolicy == policy
            assert pod.metadata.labels['xran-build'] == 'b123'
            assert pod.spec.containers.findAll { it.name != 'builder' } == original.spec.containers.findAll { it.name != 'builder' }
            assert builder.resources == original.spec.containers.find { it.name == 'builder' }.resources
            assert XranStorage.parseJson(text) == original
            checked++
        }
    }
}
['', 'ubuntu', 'ubuntu:latest\nprivileged: true', 'repo/sdk:bad;touch /tmp/escape', '-repo/sdk:tag'].each { invalid ->
    boolean rejected = false
    try { XranCompiler.validate(invalid, 'Always') } catch (IllegalArgumentException expected) { rejected = true }
    assert rejected: invalid
}
boolean rejected = false
try { XranCompiler.validate(references[0], 'Sometimes') } catch (IllegalArgumentException expected) { rejected = true }
assert rejected
println groovy.json.JsonOutput.toJson([status: 'PASS', templateCases: checked, invalidReferencesRejected: 5, invalidPolicyRejected: true])
