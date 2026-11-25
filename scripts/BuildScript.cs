// Installed by Unity Build Server before each build. Do not commit; the build
// writes this into Assets/Editor/ fresh every run (after git clean), so it's
// always present and always current regardless of what's in the repo.
#if UNITY_EDITOR
using System;
using System.Linq;
using UnityEditor;
using UnityEngine;

public static class BuildScript
{
    public static void PerformiOSBuild()
    {
        string outPath = GetArg("-customBuildPath") ?? "xcode-out";
        var scenes = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();
        if (scenes.Length == 0) throw new Exception("No enabled scenes in Build Settings.");
        var opts = new BuildPlayerOptions {
            scenes = scenes, locationPathName = outPath,
            target = BuildTarget.iOS, options = BuildOptions.None,
        };
        var report = BuildPipeline.BuildPlayer(opts);
        if (report.summary.result != UnityEditor.Build.Reporting.BuildResult.Succeeded)
            EditorApplication.Exit(1);
        Debug.Log($"Build succeeded: {report.summary.totalSize} bytes at {outPath}");
    }
    private static string GetArg(string name)
    {
        var args = Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length - 1; i++) if (args[i] == name) return args[i + 1];
        return null;
    }
}
#endif
