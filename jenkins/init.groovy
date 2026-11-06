import jenkins.model.Jenkins
import groovy.json.JsonSlurperClassic
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition
import hudson.model.ParametersDefinitionProperty
import hudson.model.ChoiceParameterDefinition
import hudson.model.StringParameterDefinition
import hudson.triggers.TimerTrigger
import hudson.security.AuthorizationMatrixProperty
import org.jenkinsci.plugins.matrixauth.inheritance.NonInheritingStrategy
import org.jenkins.plugins.lockableresources.LockableResourcesManager
import org.jenkinsci.plugins.scriptsecurity.scripts.ScriptApproval
import org.jenkinsci.plugins.scriptsecurity.scripts.languages.GroovyLanguage

def j = Jenkins.get()
// Rename the original job in place so build numbers, logs and artifacts survive.
def legacyReleaseJob = j.getItem('open5gs')
if (legacyReleaseJob != null) {
    if (j.getItem('xcn') != null) throw new IllegalStateException('xcn 任务已存在，拒绝覆盖发布历史')
    if (legacyReleaseJob.isInQueue() || legacyReleaseJob.builds.any { it.isBuilding() }) {
        throw new IllegalStateException('请等待发布构建完成后再迁移 xcn 任务名称')
    }
    legacyReleaseJob.renameTo('xcn')
}
def cfg = new JsonSlurperClassic().parseText(new File('/var/xran/site.json').text)
def platform = io.xran.jenkins.BuildPlatformConfiguration.get()
platform.initialize(cfg)
cfg.putAll(platform.effective('service'))
def support = new File('/opt/xran/storage.groovy').text
def job = j.getItem('x-ran-cloud') ?: j.createProject(WorkflowJob, 'x-ran-cloud')
def buildScript = support + '\n' + new File('/opt/xran/compiler.groovy').text + '\n' + new File('/opt/xran/pipeline.groovy').text
ScriptApproval.get().preapprove(buildScript, GroovyLanguage.get())
job.definition = new CpsFlowDefinition(buildScript, false)
// 初始化脚本会在重启/升级时再次运行，先移除历史定义，避免表单和 Action 重复。
while (job.getProperty(ParametersDefinitionProperty) != null) {
    job.removeProperty(ParametersDefinitionProperty)
}
job.addProperty(new ParametersDefinitionProperty([
    new StringParameterDefinition('COMPILER_IMAGE', cfg.builderImage as String, '编译环境'),
    new ChoiceParameterDefinition('COMPILER_IMAGE_PULL_POLICY',
        ([cfg.compilerImagePullPolicy ?: 'Always'] + ['Always', 'IfNotPresent', 'Never']).unique() as String[],
        '拉取策略'),
    new ChoiceParameterDefinition('BUILD_KIND', ['service', 'image'] as String[], 'service=服务包；image=镜像'),
    new ChoiceParameterDefinition('REF_TYPE', ['branch', 'tag', 'commit'] as String[], '引用类型'),
    new StringParameterDefinition('GIT_REF', 'main', '分支名、tag 或 commit SHA'),
    new ChoiceParameterDefinition('SERVICE_TARGET', (cfg.services + ['all', 'services_all']) as String[], '服务构建目标'),
    new ChoiceParameterDefinition('IMAGE_TARGET', cfg.images as String[], '镜像构建目标'),
    new StringParameterDefinition('IMAGE_TAG', 'latest', '镜像标签；同镜像同标签将串行执行')
]))
job.description = 'X-RAN-CLOUD 参数化构建。登录后提交构建；历史包持续保存，磁盘压力时按最早完成时间清理未标记保留产物。'
job.save()
def xcn = j.getItem('xcn') ?: j.createProject(WorkflowJob, 'xcn')
xcn.definition = new CpsFlowDefinition(buildScript, false)
while (xcn.getProperty(ParametersDefinitionProperty) != null) {
    xcn.removeProperty(ParametersDefinitionProperty)
}
xcn.addProperty(new ParametersDefinitionProperty([
    new StringParameterDefinition('COMPILER_IMAGE', cfg.builderImage as String, '编译环境'),
    new ChoiceParameterDefinition('COMPILER_IMAGE_PULL_POLICY',
        ([cfg.compilerImagePullPolicy ?: 'Always'] + ['Always', 'IfNotPresent', 'Never']).unique() as String[], '拉取策略'),
    new ChoiceParameterDefinition('REF_TYPE', ['branch', 'tag', 'commit'] as String[], '引用类型'),
    new StringParameterDefinition('GIT_REF', 'main', '分支名、tag 或 commit SHA'),
    new StringParameterDefinition('CHART_VERSION', '', 'Chart 版本，默认当天日期'),
    new StringParameterDefinition('APP_VERSION', '', '应用版本，默认当天日期')
]))
xcn.description = 'xcn 发布构建：三个镜像包和一个 Helm Chart。默认 main；版本留空按上海时区的构建日期生成。'
xcn.save()
// Repair only empty historical versions, using that build's actual metadata.
// Completed runs without metadata or with explicit values remain untouched.
def compiler = new GroovyShell(j.pluginManager.uberClassLoader).evaluate(
    support + '\n' + new File('/opt/xran/compiler.groovy').text + '\nreturn XranCompiler')
println('xcn historical versions restored: ' + compiler.restoreVersions(xcn))
def maintenance = j.getItem('storage-maintenance') ?: j.createProject(WorkflowJob, 'storage-maintenance')
def maintenanceScript = support + '\n' + new File('/opt/xran/maintenance.groovy').text
ScriptApproval.get().preapprove(maintenanceScript, GroovyLanguage.get())
maintenance.definition = new CpsFlowDefinition(maintenanceScript, false)
// 不继承 authenticated 的任务权限；Overall/Administer 仍由插件保留。
while (maintenance.getProperty(AuthorizationMatrixProperty) != null) {
    maintenance.removeProperty(AuthorizationMatrixProperty)
}
maintenance.addProperty(new AuthorizationMatrixProperty([:], new NonInheritingStrategy()))
// 每两小时一次，使用 Jenkins 哈希错开分钟与小时。
maintenance.addTrigger(new TimerTrigger('H H/2 * * *'))
def cleanupNotice = (maintenance.description ?: '').find(/(?s)最近清理：.*/)
maintenance.description = 'X-RAN-CLOUD CI：历史包持续保存；同时管理 xcn。磁盘压力时回收最早未保留产物。清理明细见 storage-maintenance。' +
    (cleanupNotice ? '\n' + cleanupNotice : '')
maintenance.save()
// systemMessage 对所有登录用户公开；管理员提示由受限任务提供。
j.systemMessage = ''
// Reconcile after job creation as well as after JCasC. Existing web settings win.
platform.initialize(cfg)
j.updateCenter.sites.clear()
j.save()
maintenance.scheduleBuild2(0)
println('X-RAN jobs initialized; concurrency=' + cfg.concurrency)
