select
    n.val,
    count(*) as cnt,
    avg(m.val) as avg_num
from mcp_explain_tool.data_nested_loop_many n
join mcp_explain_tool.data_nested_loop_inner m on m.val = n.val
where m.val > 500000
group by n.val
having count(*) > 1
order by cnt desc
limit 50;

