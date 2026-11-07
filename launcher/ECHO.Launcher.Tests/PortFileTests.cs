using EchoLauncher.Core;
using Xunit;

namespace EchoLauncher.Tests;

public sealed class PortFileTests
{
    [Theory]
    [InlineData("8970", 8970)]
    [InlineData(" 18060 ", 18060)]
    [InlineData("65535", 65535)]
    public void Valid_ports_are_parsed(string text, int expected)
    {
        Assert.Equal(expected, PortFile.Parse(text));
    }

    [Theory]
    [InlineData("")]
    [InlineData("0")]
    [InlineData("-1")]
    [InlineData("65536")]
    [InlineData("abc")]
    public void Invalid_ports_are_rejected(string text)
    {
        Assert.Null(PortFile.Parse(text));
    }

    [Fact]
    public void Read_uses_fallback_when_file_is_missing()
    {
        string root = Path.Combine(Path.GetTempPath(), "echo-port-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            Assert.Equal(8970, PortFile.Read(root));
            Assert.Equal(9999, PortFile.Read(root, 9999));
            File.WriteAllText(Path.Combine(root, PortFile.FileName), "18060\n");
            Assert.Equal(18060, PortFile.Read(root));
        }
        finally
        {
            Directory.Delete(root, recursive: true);
        }
    }
}
