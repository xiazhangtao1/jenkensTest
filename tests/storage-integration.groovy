// 仅独立验收实例：暂时保护现有版本，以两个指定测试版本验证真实空间回收。
def j=Jenkins.get()
def cfg=new JsonSlurperClassic().parseText(new File('/var/xran/site.json').text)
assert cfg.verificationInstance == true: '磁盘压力测试只能在 verificationInstance: true 的独立验收实例运行'
def jobs=['x-ran-cloud','xcn'].collect { j.getItem(it) }
def finished=jobs.collectMany { job -> job.builds.findAll { !it.isBuilding() && it.getArtifactsDir().exists() } }.sort { a,b -> (a.startTimeInMillis+a.duration) <=> (b.startTimeInMillis+b.duration) }
assert finished.size() >= 3
def previous=finished.collectEntries { [(it.externalizableId): it.isKeepLog()] }
def eligible=finished.findAll { !it.isKeepLog() && !new File(it.rootDir,'xran-cleaned.json').exists() }
assert eligible.size() >= 3
def protectedBuild=eligible[-3]
def first=eligible[-2]
def second=eligible[-1]
def running=jobs.collectMany { job -> job.builds.findAll { it.isBuilding() } }
def platform=io.xran.jenkins.BuildPlatformConfiguration.get()
def originalSettings=platform.form
def liveThreshold=false
def snapshotReservation=false
try {
    def selected=new HashMap(cfg)
    selected.temporaryStorageGiB=2
    def active=running.find()
    def workspace=[dev:XranStorage.device(j.rootDir),total:j.rootDir.totalSpace,free:j.rootDir.usableSpace]
    try {
        def edited=platform.form
        edited.revision=platform.revision
        edited.minimumFreeGiB=(originalSettings.minimumFreeGiB as int)+1
        edited.serviceTemporaryStorageGiB=9
        platform.configure((org.kohsuke.stapler.StaplerRequest2)null,edited)
        // Deliberately pass an old threshold: the helper must consult live settings.
        assert XranStorage.floor([minimumFreeGiB:1,minimumFreePercent:0],1073741824L)==
            ((originalSettings.minimumFreeGiB as long)+1L)*1073741824L
        liveThreshold=true
        if (active) {
            XranStorage.reserve(selected,active.externalizableId,active.parent.fullName,active.number,workspace)
            assert XranStorage.readState()[active.externalizableId].temporaryBytes==2L*1073741824L
            snapshotReservation=true
        }
    } finally {
        if (active) XranStorage.release(active.externalizableId)
        originalSettings.revision=platform.revision
        platform.configure((org.kohsuke.stapler.StaplerRequest2)null,originalSettings)
    }
    finished.each { it.keepLog(true) }
    first.keepLog(false); second.keepLog(false)
    [first,second].each { build ->
        def marker=new File(build.getArtifactsDir(),'pressure-fixture.bin')
        marker.parentFile.mkdirs()
        marker.withOutputStream { out ->
            byte[] bytes=new byte[1024*1024]
            new Random(37L).nextBytes(bytes)
            32.times { out.write(bytes) }
        }
    }
    def normal=XranStorage.sweep(cfg)
    assert normal.deleted.isEmpty()
    def pressured=new HashMap(cfg)
    pressured.minimumFreeBytes=j.rootDir.usableSpace+1L
    def result=XranStorage.sweep(pressured)
    assert result.deleted && result.deleted[0].build==first.externalizableId
    assert !new File(protectedBuild.rootDir,'xran-cleaned.json').exists()
    assert new File(first.rootDir,'xran-cleaned.json').exists()
    assert XranStorage.candidates().every { !it.isBuilding() && !it.isKeepLog() }
    boolean rejected=false
    pressured.minimumFreeBytes=j.rootDir.totalSpace*2L
    try { XranStorage.sweep(pressured) } catch (IllegalStateException expected) { rejected=true }
    assert rejected
    assert !new File(protectedBuild.rootDir,'xran-cleaned.json').exists()
    assert running.every { !new File(it.rootDir,'xran-cleaned.json').exists() && it.getArtifactsDir().exists() }
    assert new File(XranStorage.directory(),'cleanup.jsonl').exists()
    println(JsonOutput.toJson([oldestDeleted:first.externalizableId, protectedBuild:protectedBuild.externalizableId,
        secondDeleted:second.externalizableId, runningProtected:running.collect { it.externalizableId },
        rejectedWhenInsufficient:rejected, audit:true, liveDiskThreshold:liveThreshold,
        reservationUsesBuildSnapshot:snapshotReservation]))
} finally {
    finished.each { it.keepLog(previous[it.externalizableId]) }
    [first,second].each { new File(it.getArtifactsDir(),'pressure-fixture.bin').delete() }
}
return null
