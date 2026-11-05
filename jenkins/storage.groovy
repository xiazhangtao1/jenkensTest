import com.cloudbees.groovy.cps.NonCPS
import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import jenkins.model.Jenkins
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.SimpleFileVisitor
import java.nio.file.FileVisitResult
import java.nio.file.attribute.BasicFileAttributes

/** Controller 本地 ArtifactManager 的压力回收与持久容量预留。 */
class XranStorage {
    @NonCPS static Map parseJson(String text) {
        return (Map)new JsonSlurperClassic().parseText(text)
    }

    @NonCPS static File directory() {
        def dir = new File(Jenkins.get().rootDir, 'xran-storage')
        dir.mkdirs()
        return dir
    }

    @NonCPS static Map readState() {
        def file = new File(directory(), 'reservations.json')
        return file.exists() ? (Map)new JsonSlurperClassic().parseText(file.text) : [:]
    }

    @NonCPS static void saveState(Map value) {
        def target = new File(directory(), 'reservations.json').toPath()
        def temporary = Files.createTempFile(directory().toPath(), 'reservations-', '.tmp')
        Files.write(temporary, JsonOutput.toJson(value).getBytes('UTF-8'))
        Files.move(temporary, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
    }

    @NonCPS static Map liveState() {
        def state = readState()
        state = state.findAll { key, value ->
            def job = Jenkins.get().getItemByFullName(value.job)
            return job?.getBuildByNumber(value.number as int)?.isBuilding()
        }
        return state
    }

    @NonCPS static long device(File path) {
        return (Files.getAttribute(path.toPath(), 'unix:dev') as Number).longValue()
    }

    @NonCPS static long size(File path) {
        if (!path.exists()) return 0L
        long[] total = [0L]
        Files.walkFileTree(path.toPath(), new SimpleFileVisitor<java.nio.file.Path>() {
            @NonCPS @Override FileVisitResult visitFile(java.nio.file.Path file, BasicFileAttributes attributes) {
                total[0] += attributes.size()
                return FileVisitResult.CONTINUE
            }
        })
        return total[0]
    }

    @NonCPS static long floor(Map cfg, long total) {
        // minimumFreeBytes 供管理员诊断/隔离验收，普通用户没有配置权限。
        if (cfg.containsKey('minimumFreeBytes')) return cfg.minimumFreeBytes as long
        return Math.max((cfg.minimumFreeGiB as Number).longValue() * 1073741824L,
            (long)(total * ((cfg.minimumFreePercent as Number).doubleValue() / 100.0d)))
    }

    @NonCPS static List candidates() {
        def job = Jenkins.get().getItemByFullName('x-ran-cloud')
        if (job == null) return []
        return job.builds.findAll { run ->
            !run.isBuilding() && !run.isKeepLog() && run.getArtifactsDir().exists() &&
                !new File(run.rootDir, 'xran-cleaned.json').exists()
        }.sort { a, b -> (a.startTimeInMillis + a.duration) <=> (b.startTimeInMillis + b.duration) }
    }

    @NonCPS static Map enforce(Map cfg, Map state, Map workspace) {
        File home = Jenkins.get().rootDir
        long homeDev = device(home)
        Map reserved = [:].withDefault { 0L }
        state.values().each { reservation ->
            reserved[reservation.workspaceDev.toString()] += reservation.temporaryBytes as long
            reserved[reservation.dataDev.toString()] += reservation.archiveBytes as long
        }
        long homeReserved = reserved[homeDev.toString()] as long
        long homeFloor = floor(cfg, home.totalSpace)
        if (workspace && workspace.dev.toString() != homeDev.toString()) {
            long required = floor(cfg, workspace.total as long) + (reserved[workspace.dev.toString()] as long)
            if ((workspace.free as long) < required) {
                throw new IllegalStateException("临时盘空间不足：available=${workspace.free}, required=${required}；数据盘回收无法释放该临时盘")
            }
        }
        def deleted = []
        for (run in candidates()) {
            if (home.usableSpace >= homeFloor + homeReserved) break
            long bytes = size(run.getArtifactsDir())
            if (bytes == 0L) continue
            def event = [at: new Date().format("yyyy-MM-dd'T'HH:mm:ssXXX", TimeZone.getTimeZone('Asia/Shanghai')),
                build: run.externalizableId, description: run.description ?: '', bytes: bytes,
                reason: 'disk-pressure', availableBefore: home.usableSpace]
            run.deleteArtifacts()
            event.availableAfter = home.usableSpace
            new File(run.rootDir, 'xran-cleaned.json').text = JsonOutput.prettyPrint(JsonOutput.toJson(event))
            run.description = "[产物已于 ${event.at} 清理] " + (run.description ?: '')
            run.save()
            new File(directory(), 'cleanup.jsonl').append(JsonOutput.toJson(event) + '\n', 'UTF-8')
            deleted.add(event)
        }
        if (deleted) {
            def notice = "最近清理：${deleted[-1].at}，删除 ${deleted.size()} 个历史产物集合，" +
                "合计 ${deleted.collect { it.bytes as long }.sum()} 字节；可用 ${home.usableSpace} 字节。" +
                ' 明细见 storage-maintenance 任务和 xran-storage/cleanup.jsonl。'
            Jenkins.get().systemMessage = (Jenkins.get().systemMessage ?: '').split('最近清理：')[0].trim() + '\n' + notice
            Jenkins.get().save()
        }
        def result = [available: home.usableSpace, reserved: homeReserved, minimum: homeFloor, deleted: deleted]
        if (home.usableSpace < homeFloor + homeReserved) {
            throw new IllegalStateException('空间不足，清理后仍无法满足安全余量及在途构建预留：' + JsonOutput.toJson(result))
        }
        return result
    }

    @NonCPS static Map reserve(Map cfg, String key, String job, int number, Map workspace, long archiveBytes = 0L) {
        synchronized ('xran.storage.v1'.intern()) {
            def state = liveState()
            def before = state[key]
            state[key] = [job: job, number: number, dataDev: device(Jenkins.get().rootDir),
                workspaceDev: workspace.dev, temporaryBytes: (cfg.temporaryStorageGiB as long) * 1073741824L,
                archiveBytes: archiveBytes]
            try {
                def result = enforce(cfg, state, workspace)
                saveState(state)
                return result
            } catch (Exception error) {
                if (before) state[key] = before
                else state.remove(key)
                saveState(state)
                throw error
            }
        }
    }

    @NonCPS static void release(String key) {
        synchronized ('xran.storage.v1'.intern()) {
            def state = liveState()
            state.remove(key)
            saveState(state)
        }
    }

    @NonCPS static Map sweep(Map cfg) {
        synchronized ('xran.storage.v1'.intern()) {
            def state = liveState()
            saveState(state)
            return enforce(cfg, state, null)
        }
    }
}
