// 仅管理员可修改的受信流水线；被选择的业务分支只提供源码和原构建脚本。
def cfg = XranStorage.parseJson(new File('/var/xran/site.json').text)
def project = env.JOB_NAME as String
def isOpen5gs = project == 'open5gs'
if (!(project in ['x-ran-cloud', 'open5gs'])) error('未知构建项目')
if (isOpen5gs) cfg.putAll(cfg.open5gs as Map)
def sourceType = params.REF_TYPE as String
def ref = params.GIT_REF as String
def kind = isOpen5gs ? 'open5gs' : params.BUILD_KIND as String
def target = isOpen5gs ? 'release' : (kind == 'service' ? params.SERVICE_TARGET : params.IMAGE_TARGET) as String
def today = new Date(currentBuild.startTimeInMillis).format('yy.M.d', TimeZone.getTimeZone('Asia/Shanghai'))
def chartVersion = isOpen5gs ? ((params.CHART_VERSION as String)?.trim() ?: today) : ''
def imageTag = isOpen5gs ? ((params.APP_VERSION as String)?.trim() ?: today) : params.IMAGE_TAG as String
def offline = isOpen5gs && params.OFFLINE == true
def needsDocker = kind != 'service'
def compilerImage = (params.containsKey('COMPILER_IMAGE') ? params.COMPILER_IMAGE : cfg.builderImage) as String
def compilerPolicy = (params.containsKey('COMPILER_IMAGE_PULL_POLICY') ? params.COMPILER_IMAGE_PULL_POLICY :
    (cfg.compilerImagePullPolicy ?: 'Always')) as String
XranCompiler.validate(compilerImage, compilerPolicy)
if (!(sourceType in ['branch', 'tag', 'commit']) || !(kind in ['service', 'image', 'open5gs'])) error('非法构建类型或引用类型')
if (!isOpen5gs && !(target in (kind == 'service' ? cfg.services + ['all', 'services_all'] : cfg.images))) error('非法构建目标')
if (!ref || ref.startsWith('-') || ref.contains('\n')) error('非法 Git 引用')
if (needsDocker && !(imageTag ==~ /[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}/)) error('非法镜像标签')
if (isOpen5gs && !(chartVersion ==~ /(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?/)) error('非法 Chart 版本，请使用不带前导零的 SemVer')
def trigger = currentBuild.rawBuild.getCause(hudson.model.Cause.UserIdCause)
def started = currentBuild.startTimeInMillis
def metadata = [buildNumber: env.BUILD_NUMBER, buildUrl: env.BUILD_URL, job: env.JOB_NAME, project: project,
    userId: trigger?.userId ?: 'system', userName: trigger?.userName ?: 'system',
    kind: kind, target: target, imageTag: kind == 'image' ? imageTag : null,
    builderImage: compilerImage, builderImagePullPolicy: compilerPolicy,
    startedAt: new Date(started).format("yyyy-MM-dd'T'HH:mm:ssXXX", TimeZone.getTimeZone('Asia/Shanghai'))]
if (isOpen5gs) metadata.putAll([chartVersion: chartVersion, appVersion: imageTag, offline: offline])
trigger = null // Cause 对象不跨 Pipeline 挂起点序列化。
def reference = isOpen5gs ? "${cfg.registry}/xcn/releases:${imageTag}" : (kind == 'image' ? "${cfg.registry}/${cfg.imageRepositories[target]}:${imageTag}" : '')
def reservationKey = "${env.JOB_NAME}#${env.BUILD_NUMBER}".toString()
def execute = {
    lock(label: 'xran-build-slot', quantity: 1) {
        def pod = XranCompiler.template(new File('/var/xran/pod-' + kind + '.json').text,
            compilerImage, compilerPolicy, env.BUILD_NUMBER, project)
        def yaml = groovy.json.JsonOutput.toJson(pod)
        pod = null
        podTemplate(yaml: yaml, idleMinutes: 0, podRetention: never(), showRawYaml: false,
            slaveConnectTimeout: (cfg.agentStartupTimeoutMinutes ?: 15) * 60 as int) {
          try {
            def agentReady = false
            def executionFinished = false
            def startupDeadline = System.currentTimeMillis() + (cfg.agentStartupTimeoutMinutes ?: 15) * 60000L
            parallel('构建': {
              try {
            node(POD_LABEL) {
                timeout(time: (cfg.agentStartupTimeoutMinutes ?: 15) as int, unit: 'MINUTES') {
                    waitUntil(initialRecurrencePeriod: 3000) {
                        def info = XranCompiler.podInfo(cfg.namespace as String, env.BUILD_NUMBER, project)
                        if (info.running && info.imageId) {
                            metadata.builderImageId = info.imageId
                            metadata.builderPod = info.pod
                            agentReady = true
                            return true
                        }
                        if (info.reason) echo "编译容器等待：${info.reason} ${info.message ?: ''}"
                        return false
                    }
                }
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
                    "XRAN_CHART_VERSION=${chartVersion}", "XRAN_OFFLINE=${offline}",
                    "BUILD_MEMORY=${cfg.innerBuildMemoryGiB ?: 3}g", "BUILD_JOBS=${cfg.buildJobs ?: 2}",
                    "XRAN_BUILD_DELAY_SECONDS=${cfg.demoBuildDelaySeconds ?: 0}",
                    'TZ=Asia/Shanghai', 'MAKEFLAGS=-j4', 'CMAKE_BUILD_PARALLEL_LEVEL=4',
                    'DOCKER_HOST=unix:///var/run/xran/docker.sock',
                    'GIT_TERMINAL_PROMPT=0', 'GIT_CONFIG_NOSYSTEM=1',
                    'GIT_SSH_COMMAND=ssh -o BatchMode=yes -o StrictHostKeyChecking=yes'
                ]
                withEnv(environment) {
                    container('builder') {
                        try {
                            stage('检查编译环境') {
                                echo "编译镜像=${compilerImage}；拉取策略=${compilerPolicy}；实际镜像=${metadata.builderImageId}"
                                sh '''set -eu
                                    for tool in python3 git gcc g++ cmake pkg-config; do
                                        command -v "$tool" >/dev/null || { echo "编译镜像缺少工具：$tool" >&2; exit 1; }
                                    done
                                    test -r /opt/xran/run-build.py || { echo '编译镜像缺少 /opt/xran/run-build.py，请使用本项目制作的兼容镜像' >&2; exit 1; }
                                '''
                                if (needsDocker) sh 'command -v docker >/dev/null && test -r /opt/xran/base-images.tar'
                                if (isOpen5gs) sh 'command -v helm >/dev/null && python3 -c "import yaml" && test -r /opt/xran/open5gs-bases/images.tar'
                            }
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
                                currentBuild.description = "${metadata.startedAt} | ${metadata.userId} | ${sourceType}:${ref} | commit=${checkout.commit} | compiler=${compilerImage} (${compilerPolicy})"
                                if (isOpen5gs) currentBuild.description += " | chart=${chartVersion} app=${imageTag} offline=${offline}"
                            }
                            stage('编译与打包') {
                                if (needsDocker && cfg.registryCredentialsId) {
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
                                withEnv(["XRAN_BUILD_METADATA=${groovy.json.JsonOutput.toJson(metadata)}"]) {
                                    def status = sh(returnStatus: true, script: 'python3 /opt/xran/run-build.py collect')
                                    if (status != 0 && outcome == 'SUCCESS') {
                                        outcome = 'FAILURE'
                                        failure = new RuntimeException('产物完整性或数量校验失败')
                                    }
                                }
                                if (fileExists('collection.json')) {
                                    def collection = XranStorage.parseJson(readFile('collection.json'))
                                    if (collection.warnings) {
                                        echo collection.warnings.join('；')
                                        currentBuild.description = (currentBuild.description ?: '') + ' | ' + collection.warnings.join('；')
                                    }
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
              } finally { executionFinished = true }
            }, '等待编译环境': {
                // The Kubernetes plugin may replace a failed Pod repeatedly. Bound the
                // entire provisioning interval, without limiting compilation after readiness.
                while (!agentReady && !executionFinished) {
                    if (System.currentTimeMillis() >= startupDeadline) {
                        def info = XranCompiler.podInfo(cfg.namespace as String, env.BUILD_NUMBER, project)
                        metadata.builderStartupError = info
                        error("编译 Pod 启动超时：${compilerImage} (${compilerPolicy})；" + groovy.json.JsonOutput.toJson(info))
                    }
                    sleep(time: 3, unit: 'SECONDS')
                }
            }, failFast: true)
          } catch (Exception problem) {
              try {
                  def info = XranCompiler.podInfo(cfg.namespace as String, env.BUILD_NUMBER, project)
                  echo "编译镜像 ${compilerImage} (${compilerPolicy}) 的 Pod 状态：" + groovy.json.JsonOutput.toJson(info)
              } catch (Exception diagnostic) { echo '无法取得 Pod 诊断：' + diagnostic.message }
              throw problem
          }
        }
    }
}
timestamps {
    timeout(time: cfg.buildTimeoutMinutes as int, unit: 'MINUTES') {
        try {
            if (needsDocker) {
                def digest = java.security.MessageDigest.getInstance('SHA-256').digest(reference.getBytes('UTF-8')).encodeHex().toString()
                lock(resource: 'xran-image-' + digest) { execute() }
            } else execute()
        } catch (org.jenkinsci.plugins.workflow.steps.FlowInterruptedException interrupted) {
            currentBuild.result = 'ABORTED'
            throw interrupted
        } catch (Exception failed) {
            currentBuild.result = 'FAILURE'
            throw failed
        } finally {
            XranStorage.release(reservationKey)
            def record = new File(currentBuild.rawBuild.rootDir, 'xran-metadata.json')
            if (!record.exists()) {
                metadata.result = currentBuild.currentResult
                metadata.durationMillis = System.currentTimeMillis() - started
                record.text = groovy.json.JsonOutput.toJson(metadata)
            }
        }
    }
}
