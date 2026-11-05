/** Controller-side helpers; all returned values are plain serializable data. */
class XranCompiler {
    @com.cloudbees.groovy.cps.NonCPS
    static void validate(String image, String policy) {
        // Repository/tag/digest only: user input is never interpreted as shell or YAML.
        String component = '[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'
        String host = '[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*'
        String repository = '(?:' + host + '(?::[0-9]+)?/)?' + component + '(?:/' + component + ')*'
        String version = '(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})'
        if (image == null || !(image ==~ (repository + version))) {
            throw new IllegalArgumentException('非法编译镜像地址，请填写完整仓库路径及 :tag 或 @sha256:摘要')
        }
        if (!(policy in ['Always', 'IfNotPresent', 'Never'])) {
            throw new IllegalArgumentException('非法编译镜像拉取策略')
        }
    }

    @com.cloudbees.groovy.cps.NonCPS
    static Map template(String text, String image, String policy, String number) {
        validate(image, policy)
        def pod = XranStorage.parseJson(text)
        pod.metadata.labels['xran-build'] = 'b' + number
        def builder = pod.spec.containers.find { it.name == 'builder' }
        if (builder == null) throw new IllegalArgumentException('Pod 模板缺少 builder 容器')
        builder.image = image
        builder.imagePullPolicy = policy
        return pod
    }

    @com.cloudbees.groovy.cps.NonCPS
    static Map podInfo(String namespace, String number) {
        def cloud = jenkins.model.Jenkins.get().clouds.find {
            it instanceof org.csanchez.jenkins.plugins.kubernetes.KubernetesCloud
        }
        if (cloud == null) return [:]
        def pods = cloud.connect().pods().inNamespace(namespace).withLabel('xran-build', 'b' + number).list().items
        def pod = pods.find { it.status?.phase == 'Running' } ?: pods.find()
        if (pod == null) return [:]
        def builder = pod.status?.containerStatuses?.find { it.name == 'builder' }
        def spec = pod.spec.containers.find { it.name == 'builder' }
        return [pod: pod.metadata.name, image: spec?.image, policy: spec?.imagePullPolicy,
            imageId: builder?.imageID, running: builder?.state?.running != null,
            reason: builder?.state?.waiting?.reason ?: builder?.state?.terminated?.reason,
            message: builder?.state?.waiting?.message ?: builder?.state?.terminated?.message]
    }
}
