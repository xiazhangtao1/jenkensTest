def cfg = new groovy.json.JsonSlurperClassic().parseText(new File('/var/xran/site.json').text)
timestamps {
    def result = XranStorage.sweep(cfg)
    echo groovy.json.JsonOutput.prettyPrint(groovy.json.JsonOutput.toJson(result))
    currentBuild.description = "可用 ${result.available} 字节，预留 ${result.reserved} 字节；本次清理 ${result.deleted.size()} 个集合"
    def audit = new File(XranStorage.directory(), 'cleanup.jsonl')
    if (audit.exists()) echo '最近清理明细：\n' + audit.readLines('UTF-8').takeRight(20).join('\n')
}
