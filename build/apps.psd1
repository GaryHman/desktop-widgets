@{
  Apps = @(
    @{ Key='keep';    Src='KeepNotesWidgets';  Script='keep_widgets.py';        Exe='KeepNotesWidgets';  Name='KeepNotes Widgets';   Ver='3.0.0'; Meta=@('gpsoauth','gkeepapi') }
    @{ Key='todoist'; Src='TodoistWidgets';    Script='todoist_widgets.py';     Exe='TodoistWidgets';    Name='Todoist Widgets';     Ver='4.0.0' }
    @{ Key='bridge';  Src='..\..\Keep-Todoist Bridge'; Script='keep_todoist_bridge.py'; Exe='KeepTodoistBridge'; Name='Keep-Todoist Bridge'; Ver='1.1.0'; Meta=@('gpsoauth','gkeepapi') }
  )
}
