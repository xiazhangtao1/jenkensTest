package io.xran.jenkins;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import net.sf.json.JSONObject;

/** Small, versioned JSON schema. Never binds arbitrary submitted Java classes. */
public final class PlatformSettings {
    private PlatformSettings() {}

    /** Upgrade saved field names without replacing administrator values with defaults. */
    public static JSONObject upgradeNames(JSONObject input) {
        JSONObject result = JSONObject.fromObject(input.toString());
        for (Object name : input.keySet()) {
            String key = name.toString();
            if (key.startsWith("open5gs")) {
                String replacement = "xcn" + key.substring("open5gs".length());
                if (result.containsKey(replacement) && !java.util.Objects.equals(result.get(replacement), input.get(key))) {
                    throw new IllegalArgumentException("xcn 配置存在冲突：" + replacement);
                }
                result.put(replacement, input.get(key));
                result.remove(key);
            }
        }
        return result;
    }

    public static JSONObject defaults() {
        JSONObject s = new JSONObject();
        s.put("concurrency", 6);
        for (String p : List.of("service", "image", "xcn")) {
            s.put(p + "Cpu", 4);
            s.put(p + "MemoryGiB", 8);
            s.put(p + "TemporaryStorageGiB", 20);
        }
        s.put("imageDindMemoryGiB", 4);
        s.put("xcnDindMemoryGiB", 4);
        s.put("innerBuildMemoryGiB", 3);
        s.put("buildJobs", 2);
        s.put("compilerImage", "192.168.10.35/xnet/xran-compile:latest");
        s.put("compilerImagePullPolicy", "Always");
        s.put("xcnOffline", true);
        s.put("xranServiceSerial", true);
        s.put("buildTimeoutMinutes", 240);
        s.put("agentStartupTimeoutMinutes", 15);
        s.put("minimumFreeGiB", 50);
        s.put("minimumFreePercent", 10);
        return s;
    }

    /** Fill only newly introduced settings; keep administrator values and old audit records. */
    public static JSONObject upgradeSettings(JSONObject input) {
        JSONObject result = upgradeNames(input);
        if (!result.containsKey("xranServiceSerial")) result.put("xranServiceSerial", true);
        return result;
    }

    public static JSONObject migrate(JSONObject site) {
        JSONObject s = defaults();
        for (String key : List.of("concurrency", "compilerImagePullPolicy", "buildTimeoutMinutes",
                "agentStartupTimeoutMinutes", "minimumFreeGiB", "minimumFreePercent", "xranServiceSerial")) {
            if (site.containsKey(key)) s.put(key, site.get(key));
        }
        if (site.containsKey("builderImage")) s.put("compilerImage", site.get("builderImage"));
        for (String p : List.of("service", "image", "xcn")) {
            s.put(p + "Cpu", site.optInt("buildCpuLimit", 4));
            s.put(p + "MemoryGiB", site.optInt("buildMemoryGiB", 8));
            s.put(p + "TemporaryStorageGiB", site.optInt("temporaryStorageGiB", 20));
        }
        s.put("imageDindMemoryGiB", site.optInt("dindMemoryGiB", 4));
        s.put("xcnDindMemoryGiB", site.optInt("dindMemoryGiB", 4));
        JSONObject open = site.optJSONObject("xcn");
        if (open == null) open = site.optJSONObject("open5gs");
        if (open != null) {
            s.put("xcnOffline", open.optBoolean("offline", true));
            s.put("innerBuildMemoryGiB", open.optInt("innerBuildMemoryGiB", 3));
            s.put("buildJobs", open.optInt("buildJobs", 2));
        }
        return validate(s);
    }

    public static JSONObject validate(JSONObject input) {
        JSONObject s = defaults();
        for (Object name : s.keySet()) {
            String key = name.toString();
            if (!input.containsKey(key)) throw new IllegalArgumentException("缺少设置：" + key);
            Object value = input.get(key);
            if (s.get(key) instanceof Number) {
                if (!value.toString().matches("[0-9]+")) throw new IllegalArgumentException(key + " 必须为整数");
                s.put(key, Integer.parseInt(value.toString()));
            } else if (s.get(key) instanceof Boolean) {
                if (!(value instanceof Boolean)) throw new IllegalArgumentException(key + " 必须为布尔值");
                s.put(key, value);
            } else s.put(key, value.toString().trim());
        }
        range(s, "concurrency", 1, 32);
        for (String p : List.of("service", "image", "xcn")) {
            range(s, p + "Cpu", 2, 128);
            range(s, p + "MemoryGiB", p.equals("service") ? 2 : 4, 1024);
            range(s, p + "TemporaryStorageGiB", 1, 1024);
            if (!p.equals("service")) {
                range(s, p + "DindMemoryGiB", 2, s.getInt(p + "MemoryGiB") - 2);
            }
            allocation(s, p); // validates requests <= limits and exact totals
        }
        range(s, "innerBuildMemoryGiB", 1, s.getInt("xcnDindMemoryGiB") - 1);
        range(s, "buildJobs", 1, 128);
        range(s, "buildTimeoutMinutes", 1, 10080);
        range(s, "agentStartupTimeoutMinutes", 1, Math.min(120, s.getInt("buildTimeoutMinutes")));
        range(s, "minimumFreeGiB", 1, 1048576);
        range(s, "minimumFreePercent", 0, 79);
        String component = "[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*";
        String host = "[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*";
        String image = "(?:" + host + "(?::[0-9]+)?/)?" + component + "(?:/" + component
                + ")*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})";
        if (!Pattern.matches(image, s.getString("compilerImage"))) throw new IllegalArgumentException("编译环境须包含合法 tag 或 sha256 摘要");
        if (!List.of("Always", "IfNotPresent", "Never").contains(s.getString("compilerImagePullPolicy"))) {
            throw new IllegalArgumentException("非法拉取策略");
        }
        return s;
    }

    private static void range(JSONObject s, String key, int minimum, int maximum) {
        int n = s.getInt(key);
        if (n < minimum || n > maximum) throw new IllegalArgumentException(key + " 范围为 " + minimum + ".." + maximum);
    }

    public static Map<String, Object> allocation(JSONObject s, String p) {
        if (!List.of("service", "image", "xcn").contains(p)) throw new IllegalArgumentException("未知资源配置");
        int cpu = s.getInt(p + "Cpu") * 1000, mem = s.getInt(p + "MemoryGiB") * 1024;
        int temp = s.getInt(p + "TemporaryStorageGiB") * 1024;
        boolean docker = !p.equals("service"), open = p.equals("xcn");
        int dind = docker ? s.getInt(p + "DindMemoryGiB") * 1024 : 0;
        int workspace = docker ? (open ? Math.min(4096, temp / 2) : temp / 2) : temp - 256;
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("jnlp", resources(100, 256, 64, 250, 512, 256));
        result.put("builder", resources(500, Math.min(1024, mem - (docker ? 1536 : 512)),
                Math.min(1024, workspace), open ? 500 : cpu - (docker ? 1500 : 250),
                mem - dind - 512, workspace));
        if (docker) result.put("dind", resources(250, 256, Math.min(1024, temp - workspace - 256),
                open ? cpu - 750 : 1250, dind, temp - workspace - 256));
        return result;
    }

    private static Map<String, Object> resources(int rc, int rm, int rt, int lc, int lm, int lt) {
        if (rc > lc || rm > lm || rt > lt || Math.min(lc, Math.min(lm, lt)) <= 0) {
            throw new IllegalArgumentException("Pod 资源预算不足，无法满足内部容器 requests/limits");
        }
        return Map.of("requests", Map.of("cpu", rc + "m", "memory", rm + "Mi", "ephemeral-storage", rt + "Mi"),
                "limits", Map.of("cpu", lc + "m", "memory", lm + "Mi", "ephemeral-storage", lt + "Mi"));
    }

    public static Map<String, Object> runtime(JSONObject s, String p) {
        Map<String, Object> result = new LinkedHashMap<>();
        for (String key : List.of("concurrency", "buildTimeoutMinutes", "agentStartupTimeoutMinutes",
                "minimumFreeGiB", "minimumFreePercent", "compilerImagePullPolicy", "innerBuildMemoryGiB", "buildJobs", "xranServiceSerial")) {
            result.put(key, s.get(key));
        }
        result.put("builderImage", s.getString("compilerImage"));
        result.put("buildCpuLimit", s.getInt(p + "Cpu"));
        result.put("buildMemoryGiB", s.getInt(p + "MemoryGiB"));
        result.put("temporaryStorageGiB", s.getInt(p + "TemporaryStorageGiB"));
        result.put("dindMemoryGiB", p.equals("service") ? 0 : s.getInt(p + "DindMemoryGiB"));
        result.put("xcnOffline", s.getBoolean("xcnOffline"));
        return result;
    }
}
