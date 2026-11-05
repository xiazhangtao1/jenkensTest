// 仅管理员可修改的受信流水线；被选择的业务分支只提供源码和原构建脚本。
def cfg = XranStorage.parseJson(new File('/var/xran/site.json').text)
def sourceType = params.REF_TYPE as String
def ref = params.GIT_REF as String
def kind = params.BUILD_KIND as String
def target = (kind == 'service' ? params.SERVICE_TARGET : params.IMAGE_TARGET) as String
def imageTag = params.IMAGE_TAG as String
if (!(sourceType in ['branch', 'tag', 'commit']) || !(kind in ['service', 'image'])) error('非法构建类型或引用类型')
if (!(target in (kind == 'service' ? cfg.services + ['all', 'services_all'] : cfg.images))) error('非法构建目标')
if (!ref || ref.startsWith('-') || ref.contains('\n')) error('非法 Git 引用')
if (kind == 'image' && !(imageTag ==~ /[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}/)) error('非法镜像标签')
def trigger = currentBuild.rawBuild.getCause(hudson.model.Cause.UserIdCause)
def started = currentBuild.startTimeInMillis
def metadata = [buildNumber: env.BUILD_NUMBER, buildUrl: env.BUILD_URL, job: env.JOB_NAME,
    userId: trigger?.userId ?: 'system', userName: trigger?.userName ?: 'system',
    kind: kind, target: target, imageTag: kind == 'image' ? imageTag : null,
    startedAt: new Date(started).format("yyyy-MM-dd'T'HH:mm:ssXXX", TimeZone.getTimeZone('Asia/Shanghai'))]
trigger = null // Cause 对象不跨 Pipeline 挂起点序列化。
def reference = kind == 'image' ? "${cfg.registry}/${cfg.imageRepositories[target]}:${imageTag}" : ''
def reservationKey = "${env.JOB_NAME}#${env.BUILD_NUMBER}".toString()
def execute = {
    lock(label: 'xran-build-slot', quantity: 1) {
        def yaml = new File('/var/xran/pod-' + kind + '.yaml').text.replace('XRAN_BUILD_NUMBER', env.BUILD_NUMBER)
        podTemplate(yaml: yaml, idleMinutes: 0, podRetention: never(), showRawYaml: false) {
            node(POD_LABEL) {
                def workspaceStats = {
                    return XranStorage.parseJson(sh(returnStdout: true, script:
                        '''python3 -c 'import os,shutil,json; s=shutil.disk_usage("."); print(json.dumps({"dev":os.stat(".").st_dev,"total":s.total,"free":s.free}))' ''').trim())
                }
                def outcome = 'SUCCESS'
                def failure = null
                def environment = [
                    "XRAN_GIT_URL=${cfg.gitUrl}", "XRAN_REF_TYPE=${sourceType}", "XRAN_REF=${ref}",
                    "XRAN_KIND=${kind}", "XRAN_TARGET=${target}", "XRAN_IMAGE_TAG=${imageTag}",
                    "XRAN_REGISTRY=${cfg.registry}", "IMAGE_REGISTRY=${cfg.registry}",
                    "XRAN_IMAGE_REFERENCE=${reference}", "XRAN_SERVICES=${groovy.json.JsonOutput.toJson(cfg.services)}",
                    "XRAN_BUILD_DELAY_SECONDS=${cfg.demoBuildDelaySeconds ?: 0}",
                    'TZ=Asia/Shanghai', 'MAKEFLAGS=-j4', 'CMAKE_BUILD_PARALLEL_LEVEL=4',
                    'DOCKER_HOST=unix:///var/run/xran/docker.sock',
                    'GIT_TERMINAL_PROMPT=0', 'GIT_CONFIG_NOSYSTEM=1',
                    'GIT_SSH_COMMAND=ssh -o BatchMode=yes -o StrictHostKeyChecking=yes'
                ]
                withEnv(environment) {
                    container('builder') {
                        try {
                            stage('磁盘检查') { echo groovy.json.JsonOutput.toJson(XranStorage.reserve(cfg,
                                reservationKey, env.JOB_NAME, env.BUILD_NUMBER as int, workspaceStats())) }
                            stage('检出指定版本') {
                                def checkoutCommand = { sh 'python3 /opt/xran/run-build.py checkout' }
                                if (cfg.gitCredentialsId) {
                                    if (cfg.gitUrl.startsWith('http')) {
                                        withCredentials([gitUsernamePassword(credentialsId: cfg.gitCredentialsId, gitToolName: 'Default')]) { checkoutCommand() }
                                    } else {
                                        sshagent(credentials: [cfg.gitCredentialsId]) { checkoutCommand() }
                                    }
                                } else checkoutCommand()
                                def checkout = XranStorage.parseJson(readFile('checkout.json'))
                                metadata.putAll(checkout)
                                currentBuild.displayName = "#${env.BUILD_NUMBER} ${target} ${checkout.commit.take(9)}"
                                currentBuild.description = "${metadata.startedAt} | ${metadata.userId} | ${sourceType}:${ref} | commit=${checkout.commit}"
                            }
                            stage('编译与打包') {
                                if (kind == 'image' && cfg.registryCredentialsId) {
                                    withCredentials([usernamePassword(credentialsId: cfg.registryCredentialsId,
                                        usernameVariable: 'REGISTRY_USER', passwordVariable: 'REGISTRY_PASSWORD')]) {
                                        sh '''set +x
                                            for i in $(seq 1 120); do docker info >/dev/null 2>&1 && break; sleep 1; done
                                            printf '%s' "$REGISTRY_PASSWORD" | docker login "$XRAN_REGISTRY" --username "$REGISTRY_USER" --password-stdin
                                            python3 /opt/xran/run-build.py build
                                        '''
                                    }
                                } else sh 'python3 /opt/xran/run-build.py build'
                            }
                        } catch (org.jenkinsci.plugins.workflow.steps.FlowInterruptedException interrupted) {
                            outcome = 'ABORTED'
                            failure = interrupted
                        } catch (Exception error) {
                            outcome = 'FAILURE'
                            failure = error
                        } finally {
                            stage('归档与构建信息') {
                                metadata.result = outcome
                                metadata.durationMillis = System.currentTimeMillis() - started
                                metadata.builderImage = cfg.builderImage
                                metadata.builderImageId = cfg.imageIds?.get(cfg.builderImage)
                                withEnv(["XRAN_BUILD_METADATA=${groovy.json.JsonOutput.toJson(metadata)}"]) {
                                    def status = sh(returnStatus: true, script: 'python3 /opt/xran/run-build.py collect')
                                    if (status != 0 && outcome == 'SUCCESS') {
                                        outcome = 'FAILURE'
                                        failure = new RuntimeException('产物完整性或数量校验失败')
                                    }
                                }
                                if (fileExists('collection.json')) {
                                    def collection = XranStorage.parseJson(readFile('collection.json'))
                                    echo groovy.json.JsonOutput.toJson(XranStorage.reserve(cfg, reservationKey,
                                        env.JOB_NAME, env.BUILD_NUMBER as int, workspaceStats(), collection.bytes as long))
                                    archiveArtifacts(artifacts: 'publish/*', allowEmptyArchive: false, fingerprint: false)
                                    // 回收大包后仍可查阅的小型构建元数据。
                                    new File(currentBuild.rawBuild.rootDir, 'xran-metadata.json').text = readFile('publish/metadata.json')
                                }
                            }
                            XranStorage.release(reservationKey)
                        }
                        if (failure) {
                            currentBuild.result = outcome
                            throw failure
                        }
                    }
                }
            }
        }
    }
}
timestamps {
    timeout(time: cfg.buildTimeoutMinutes as int, unit: 'MINUTES') {
        try {
            if (kind == 'image') {
                def digest = java.security.MessageDigest.getInstance('SHA-256').digest(reference.getBytes('UTF-8')).encodeHex().toString()
                lock(resource: 'xran-image-' + digest) { execute() }
            } else execute()
        } finally { XranStorage.release(reservationKey) }
    }
}
