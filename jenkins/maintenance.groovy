def cfg = new groovy.json.JsonSlurperClassic().parseText(new File('/var/xran/site.json').text)
timestamps {
    def result = XranStorage.sweep(cfg)
    echo groovy.json.JsonOutput.prettyPrint(groovy.json.JsonOutput.toJson(result))
    currentBuild.description = "可用 ${result.available} 字节，预留 ${result.reserved} 字节；本次清理 ${result.deleted.size()} 个产物集合、${result.gitCache.deleted.size()} 个 Git 缓存条目"
    def audit = new File(XranStorage.directory(), 'cleanup.jsonl')
    if (audit.exists()) echo '最近清理明细：\n' + audit.readLines('UTF-8').takeRight(20).join('\n')
    def cacheAudit = new File(XranStorage.directory(), 'git-cache-cleanup.jsonl')
    if (cacheAudit.exists()) echo '最近 Git 缓存回收：\n' + cacheAudit.readLines('UTF-8').takeRight(20).join('\n')
}
