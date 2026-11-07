using System.Diagnostics;
using System.Runtime.InteropServices;
using EchoLauncher.Core;

namespace EchoLauncher;

/// <summary>
/// Keeps the whole ECHO process tree inside a Windows job object.
///
/// Why: pythonw.exe spawns the sidebar (detached), the local backend and optional DSH.
/// When ECHO exits (or this launcher is killed) nothing should be left listening on
/// 8970/8900 or hanging on the right edge of the screen.  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
/// makes that a kernel guarantee instead of a best-effort list of taskkill commands.
/// </summary>
internal sealed class JobObject : IDisposable
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;

    private IntPtr _handle;

    private JobObject(IntPtr handle) => _handle = handle;

    public static JobObject? TryCreate(LauncherLog log)
    {
        IntPtr handle = NativeMethods.CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero)
        {
            log.Write($"job object: CreateJobObject failed ({Marshal.GetLastWin32Error()})");
            return null;
        }

        var info = new JobObjectExtendedLimitInformationStruct
        {
            BasicLimitInformation = new JobObjectBasicLimitInformation
            {
                LimitFlags = JobObjectLimitKillOnJobClose,
            },
        };

        int size = Marshal.SizeOf<JobObjectExtendedLimitInformationStruct>();
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(info, buffer, fDeleteOld: false);
            if (!NativeMethods.SetInformationJobObject(handle, JobObjectExtendedLimitInformation, buffer, (uint)size))
            {
                log.Write($"job object: SetInformationJobObject failed ({Marshal.GetLastWin32Error()})");
                NativeMethods.CloseHandle(handle);
                return null;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }

        return new JobObject(handle);
    }

    public bool Assign(Process process)
    {
        if (_handle == IntPtr.Zero) return false;
        try
        {
            return NativeMethods.AssignProcessToJobObject(_handle, process.Handle);
        }
        catch
        {
            return false;
        }
    }

    public void Dispose()
    {
        if (_handle == IntPtr.Zero) return;
        NativeMethods.CloseHandle(_handle);
        _handle = IntPtr.Zero;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformationStruct
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }
}
