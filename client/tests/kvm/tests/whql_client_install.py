import logging, time, os, re
from autotest_lib.client.common_lib import error
import kvm_subprocess, kvm_test_utils, kvm_utils, rss_file_transfer


def run_whql_client_install(test, params, env):
    """
    WHQL DTM client installation:
    1) Log into the guest (the client machine) and into a DTM server machine
    2) Stop the DTM client service (wttsvc) on the client machine
    3) Delete the client machine from the server's data store
    4) Rename the client machine (give it a randomly generated name)
    5) Move the client machine into the server's workgroup
    6) Reboot the client machine
    7) Install the DTM client software

    @param test: kvm test object
    @param params: Dictionary with the test parameters
    @param env: Dictionary with test environment.
    """
    vm = kvm_test_utils.get_living_vm(env, params.get("main_vm"))
    session = kvm_test_utils.wait_for_login(vm, 0, 240)

    # Collect test params
    server_address = params.get("server_address")
    server_shell_port = int(params.get("server_shell_port"))
    server_file_transfer_port = int(params.get("server_file_transfer_port"))
    server_studio_path = params.get("server_studio_path", "%programfiles%\\ "
                                    "Microsoft Driver Test Manager\\Studio")
    server_username = params.get("server_username")
    server_password = params.get("server_password")
    dsso_delete_machine_binary = params.get("dsso_delete_machine_binary",
                                            "deps/whql_delete_machine_15.exe")
    dsso_delete_machine_binary = kvm_utils.get_path(test.bindir,
                                                    dsso_delete_machine_binary)
    install_timeout = float(params.get("install_timeout", 600))
    install_cmd = params.get("install_cmd")
    wtt_services = params.get("wtt_services")

    # Stop WTT service(s) on client
    for svc in wtt_services.split():
        kvm_test_utils.stop_windows_service(session, svc)

    # Copy dsso_delete_machine_binary to server
    rss_file_transfer.upload(server_address, server_file_transfer_port,
                             dsso_delete_machine_binary, server_studio_path,
                             timeout=60)

    # Open a shell session with server
    server_session = kvm_utils.remote_login("nc", server_address,
                                            server_shell_port, "", "",
                                            session.prompt, session.linesep)

    # Get server and client information
    cmd = "echo %computername%"
    server_name = server_session.get_command_output(cmd).strip()
    client_name = session.get_command_output(cmd).strip()
    cmd = "wmic computersystem get domain"
    server_workgroup = server_session.get_command_output(cmd).strip()
    server_workgroup = server_workgroup.splitlines()[-1]
    regkey = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
    cmd = "reg query %s /v Domain" % regkey
    o = server_session.get_command_output(cmd).strip().splitlines()[-1]
    try:
        server_dns_suffix = o.split(None, 2)[2]
    except IndexError:
        server_dns_suffix = ""

    # Delete the client machine from the server's data store (if it's there)
    server_session.get_command_output("cd %s" % server_studio_path)
    cmd = "%s %s %s" % (os.path.basename(dsso_delete_machine_binary),
                        server_name, client_name)
    server_session.get_command_output(cmd, print_func=logging.info)
    server_session.close()

    # Rename the client machine
    client_name = "autotest_%s" % kvm_utils.generate_random_string(4)
    logging.info("Renaming client machine to '%s'" % client_name)
    cmd = ('wmic computersystem where name="%%computername%%" rename name="%s"'
           % client_name)
    if session.get_command_status(cmd, timeout=600) != 0:
        raise error.TestError("Could not rename the client machine")

    # Join the server's workgroup
    logging.info("Joining workgroup '%s'" % server_workgroup)
    cmd = ('wmic computersystem where name="%%computername%%" call '
           'joindomainorworkgroup name="%s"' % server_workgroup)
    if session.get_command_status(cmd, timeout=600) != 0:
        raise error.TestError("Could not change the client's workgroup")

    # Set the client machine's DNS suffix
    logging.info("Setting DNS suffix to '%s'" % server_dns_suffix)
    cmd = 'reg add %s /v Domain /d "%s" /f' % (regkey, server_dns_suffix)
    if session.get_command_status(cmd, timeout=300) != 0:
        raise error.TestError("Could not set the client's DNS suffix")

    # Reboot
    session = kvm_test_utils.reboot(vm, session)

    # Access shared resources on the server machine
    logging.info("Attempting to access remote share on server")
    cmd = r"net use \\%s /user:%s %s" % (server_name, server_username,
                                         server_password)
    end_time = time.time() + 120
    while time.time() < end_time:
        s = session.get_command_status(cmd)
        if s == 0:
            break
        time.sleep(5)
    else:
        raise error.TestError("Could not access server share from client "
                              "machine")

    # Install
    logging.info("Installing DTM client (timeout=%ds)", install_timeout)
    install_cmd = r"cmd /c \\%s\%s" % (server_name, install_cmd.lstrip("\\"))
    if session.get_command_status(install_cmd, timeout=install_timeout) != 0:
        raise error.TestError("Client installation failed")

    session.close()