def _sleep(ctx):
   out_file = ctx.actions.declare_file("%s.generated" % ctx.attr.name)
   ctx.actions.run_shell(

        command = "sleep 120 && echo $(date) >> %s" % (out_file.path),
        inputs = ctx.files.srcs,
        outputs= [out_file],
    )
   return [DefaultInfo(files = depset([out_file]))]

sleep = rule(
    implementation = _sleep,
     attrs = {
        "srcs": attr.label_list(
            mandatory = True,
            allow_files = True,
        ),
    },
)
